#!/bin/bash
# Ship the control plane to the fleet host.
#
#   deploy.sh <host> [ssh-key]              build this commit on the host, switch to it, prove it
#   deploy.sh --rollback <host> [ssh-key]   put back the build that was serving before the last
#                                           deploy - no source, no build, nothing to check out
#       RESTORE_DB=<backup file name>       ...and put that database backup back too (the fleet
#                                           is stopped while it is copied)
#   ROLLBACK=1 deploy.sh <host>             deploy this commit even though it does not contain the
#                                           commit that is live (a deliberate rollback by rebuild)
#   DOMAIN=<fqdn>                           the name the live checks ask (default fleet.scine.online)
#
# The image is built ON THE HOST, not here. Building locally would need Docker on the Mac
# (it is not installed) and would then stream a ~200 MB image over ssh on every deploy. The
# source is a few hundred KB, and the host already has Docker because provision.sh put it
# there — so we send source and build where it runs.
#
# ORDER MATTERS (2026-09-28). Nothing that is live changes until everything that can fail
# without an outage has passed: the identity and ancestry checks, the image build, the database
# backup, and validation of the new compose file and Caddyfile. Before this the compose file and
# Caddyfile were copied over the live ones first, a failed build went unnoticed (`| tail -1`
# swallowed its exit status), an invalid Caddyfile was only a warning, and the host could be
# left half-updated while the script reported a commit mismatch instead of the real cause.
set -euo pipefail
MODE=deploy
if [ "${1:-}" = "--rollback" ]; then MODE=rollback; shift; fi
HOST="${1:?usage: deploy.sh [--rollback] <host-or-ip> [ssh-key]}"
KEY="${2:-$HOME/.ssh/netbridge-fleet.pem}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CP="$(cd "$HERE/../.." && pwd)"                       # control-plane/
REPO="$CP/.."
S="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$HOST"
R=/opt/netbridge                                      # the compose project on the host
D="https://${DOMAIN:-fleet.scine.online}"
LT="$(mktemp -d)"; trap 'rm -rf "$LT"' EXIT

say(){ echo "[deploy] $*"; }
err(){ echo "[deploy] $*" >&2; }
field(){ printf '%s\n' "$1" | sed -n "/^$2=/{s/^$2=//p;q;}"; }

# When something does not come up, show what the host says. Without this the operator got
# "live server reports X" and had to go and type docker commands on the box, mid-outage, to
# learn that the new build was crashing on start.
host_logs() {
  err "---- from the host: docker compose ps, then the last 60 lines of ${1:-fleet} ----"
  $S "cd $R && sudo docker compose ps; sudo docker compose logs --tail 60 ${1:-fleet}" >&2 2>&1 || true
}

healthz_sha() {
  curl -fsS -m 10 "$D/healthz" 2>/dev/null | sed -n 's/.*"git_sha" *: *"\([^"]*\)".*/\1/p' || true
}

# The fleet container as the HOST sees it. This still answers when the app does not (a
# crash-looping build gets a 502 from Caddy): an image carries its commit in its environment.
host_state() {
  $S bash -s -- "$R" 2>/dev/null <<'EOF' || true
R="$1"
if [ -f "$R/.env" ]; then echo env=yes; else echo env=no; fi
envsha(){ sed -n 's/^CONTROL_PLANE_GIT_SHA=//p'; }
if sudo docker image inspect netbridge-fleet:latest >/dev/null 2>&1; then
  echo "latest=$(sudo docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' netbridge-fleet:latest | envsha)"
fi
cd "$R" 2>/dev/null && [ -f docker-compose.yml ] || exit 0
c="$(sudo docker compose ps -a -q fleet 2>/dev/null | head -1)"
[ -n "$c" ] || exit 0
echo "state=$(sudo docker inspect -f '{{.State.Status}}' "$c")"
echo "image=$(sudo docker inspect -f '{{.Image}}' "$c")"
echo "sha=$(sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" | envsha)"
EOF
}

# PROVE IT. `docker compose ps` says a container is up, which is the same class of green
# light as `{"ok": true}` -- it does not say the NEW code is serving. Ask the live server
# what it is, and fail if it is not what we just sent.
verify_live() {
  local want="$1" live="" i
  say "verifying what is actually live"
  for i in $(seq 1 20); do          # a restarting app answers 502 for a few seconds
    live="$(healthz_sha)"
    [ "$live" = "$want" ] && break
    sleep 3
  done
  if [ -z "$live" ]; then
    err "FAILED: the live server is not reporting a git_sha. Most likely the new build is not"
    err "        answering - crashing on start (see the logs below) - or DOMAIN ($D) is not"
    err "        this host. Do NOT record this deploy as verified."
    host_logs
    exit 1
  fi
  if [ "$live" != "$want" ]; then
    err "FAILED: live server reports $live but we deployed $want."
    err "        The deploy did not take effect. Investigate before assuming it worked."
    host_logs
    exit 1
  fi
  say "live commit  $live  (matches)"
}

verify_stream_closed() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$D/admin/stream" || true)"
  if [ "$code" = "401" ]; then say "/admin/stream refuses anonymous callers (401)"
  else err "FAILED: /admin/stream answered '$code' to an anonymous caller (want 401)."; exit 1; fi
}

# ---------------------------------------------------------------------------------------
# WHAT IS BEING DEPLOYED, AND CAN WE PROVE IT AFTERWARDS?
#
# Until 26 Aug 2026 this script sent a tarball, built it on the host and reported success --
# and nothing anywhere recorded WHICH COMMIT that was. The consequences were not theoretical:
# /docs was closed in source and stayed open in production for days, and the only way anyone
# found out was by probing the live server's behaviour. "Fixed in the repo" and "fixed in
# production" were indistinguishable from outside.
#
# Worse, it would cheerfully deploy a DIRTY working tree, producing a running control plane
# that matched no commit at all.
#
# Checked before anything talks to the host, so a dirty tree is refused without touching it.
if [ "$MODE" = deploy ]; then
  GIT_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
  DIRTY="$(git -C "$REPO" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  BUILD_ID="${BUILD_ID:-manual-$(date -u +%Y%m%d%H%M%S)}"

  if [ "$DIRTY" != "0" ] && [ "${ALLOW_DIRTY:-}" != "1" ]; then
    err "REFUSING: the working tree has $DIRTY uncommitted change(s)."
    err "         Deploying now would put code in production that matches no commit, and"
    err "         nothing afterwards could tell you what is running."
    err "         Commit first, or re-run with ALLOW_DIRTY=1 to deploy deliberately."
    exit 1
  fi
  [ "$DIRTY" != "0" ] && GIT_SHA="${GIT_SHA}-dirty"

  say "commit   $GIT_SHA  (branch $(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?'))"
  say "build id $BUILD_ID"
  if [ -z "$(git -C "$REPO" branch -r --contains HEAD 2>/dev/null)" ]; then
    err "WARNING: this commit is on no remote-tracking branch - it exists only in local clones, so"
    err "         nobody else can see or rebuild what is about to be live."
  fi
fi

# ---------------------------------------------------------------------------------------
# WHAT IS ON THE HOST NOW?
HS="$(host_state)"
case "$HS" in
  *env=*) ;;
  *) err "REFUSING: could not reach ubuntu@$HOST over ssh - nothing was changed."; exit 1 ;;
esac
if [ "$(field "$HS" env)" != yes ]; then
  err "REFUSING: $R/.env does not exist on the host. Without it every setting in the compose file"
  err "          is empty (Caddy would start with no site address). Create it from env.example"
  err "          first - README, bring-up step 4. Nothing was changed."
  exit 1
fi
HOST_STATE="$(field "$HS" state)"; HOST_IMAGE="$(field "$HS" image)"
HOST_SHA="$(field "$HS" sha)"; LATEST_SHA="$(field "$HS" latest)"
LIVE_HEALTH="$(healthz_sha)"
SERVING=0     # the fleet container is up AND the domain is answering with its commit
[ "$HOST_STATE" = running ] && [ -n "$HOST_SHA" ] && [ "$LIVE_HEALTH" = "$HOST_SHA" ] && SERVING=1
say "on the host  fleet ${HOST_STATE:-absent}, commit ${HOST_SHA:-none}; $D answers ${LIVE_HEALTH:-nothing}"
if [ -n "$HOST_SHA" ] && [ -n "$LIVE_HEALTH" ] && [ "$HOST_SHA" != "$LIVE_HEALTH" ]; then
  err "WARNING: $D is serving $LIVE_HEALTH but the fleet container on $HOST runs $HOST_SHA."
  err "         Is DOMAIN pointing at this host? The checks after the deploy will fail if not."
fi

# ---------------------------------------------------------------------------------------
# ROLLBACK: put back the build that was serving before the last deploy.
#
# Every deploy tags the build it replaces as netbridge-fleet:previous - but only when that build
# was actually serving. A crash-looping build never becomes "previous", so rolling back after two
# failed deploys in a row still lands on the last one that worked. Until 2026-09-28 the old image
# was simply overwritten, and the only way back was a clean checkout of the old commit and a full
# rebuild - which the backup step then refused, because the crash-looping app could not be exec'd.
if [ "$MODE" = rollback ]; then
  PREV="$($S "sudo docker image inspect -f '{{.Id}}' netbridge-fleet:previous 2>/dev/null" || true)"
  if [ -z "$PREV" ]; then
    err "REFUSING: no previous build is recorded on this host (netbridge-fleet:previous does not exist)."
    err "          Roll back by checking out the commit to go back to and running:"
    err "              ROLLBACK=1 $0 $HOST"
    exit 1
  fi
  PREV_SHA="$($S "sudo docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' netbridge-fleet:previous" \
              | sed -n 's/^CONTROL_PLANE_GIT_SHA=//p' || true)"
  if [ "$PREV" = "$HOST_IMAGE" ] && [ "$SERVING" = 1 ] && [ -z "${RESTORE_DB:-}" ]; then
    say "the fleet is already serving the previous build ($PREV_SHA) - nothing to do"
    exit 0
  fi
  if [ -n "${RESTORE_DB:-}" ] && ! [[ "$RESTORE_DB" =~ ^bridge\.db\.pre-[A-Za-z0-9._-]+$ ]]; then
    err "REFUSING: RESTORE_DB must be a backup file name as a deploy prints it (bridge.db.pre-...)."
    exit 1
  fi
  say "rolling back  ${HOST_SHA:-?} -> ${PREV_SHA:-?}"
  $S "sudo docker tag netbridge-fleet:previous netbridge-fleet:latest" \
    || { err "FAILED: could not retag netbridge-fleet:previous as latest - nothing was changed."; exit 1; }
  if [ -n "${RESTORE_DB:-}" ]; then
    SAFETY="bridge.db.pre-restore-$(date -u +%Y%m%d%H%M%S)"
    say "restoring the database from /data/$RESTORE_DB (the one it replaces is kept as /data/$SAFETY)"
    # The script travels as a file and then on stdin (docker compose run -T -> python -), so no
    # quoting can mangle it. The fleet is STOPPED first: copying a database under a running app
    # that holds it open can interleave with its writes. Both copies use SQLite's backup API over
    # ordinary read-write connections, not cp: the copy is consistent, and a transaction that a
    # crash left half-done is rolled back first instead of being copied.
    $S "cat > $R/.db-restore.py" <<'PY'
import os, sqlite3, sys
live, backup, safety = sys.argv[1:4]
if not os.path.exists(backup):
    sys.exit("[deploy] no such backup: %s" % backup)
if os.path.exists(live):
    s, d = sqlite3.connect(live), sqlite3.connect(safety)
    s.backup(d); d.close(); s.close()
b, l = sqlite3.connect(backup), sqlite3.connect(live)
b.backup(l)
print("[deploy] restored: %d devices" % l.execute("select count(*) from devices").fetchone()[0])
l.close(); b.close()
PY
    if ! $S bash -s -- "$R" "$RESTORE_DB" "$SAFETY" <<'EOF'
set -e
cd "$1"
sudo docker compose stop fleet
sudo docker compose run --rm --no-deps -T fleet python - /data/bridge.db "/data/$2" "/data/$3" < .db-restore.py
EOF
    then
      err "FAILED: the database was NOT restored (above). Starting the previous build on the database"
      err "        the fleet already had - check the backup name and run the restore again."
      $S "cd $R && sudo docker compose up -d" || true
      exit 1
    fi
  fi
  $S "cd $R && sudo docker compose up -d && sleep 5 && sudo docker compose ps" \
    || { err "FAILED: docker compose up did not complete."; host_logs; exit 1; }
  verify_live "${PREV_SHA:-unknown}"
  verify_stream_closed
  say "the proxy config and compose file were left as they are (Caddyfile.prev on the host holds the"
  say "one from before the last deploy, if that needs undoing too)"
  say "done"
  exit 0
fi

# ---------------------------------------------------------------------------------------
# DOES THIS COMMIT CONTAIN WHAT IS LIVE? (2026-09-28)
#
# "Working tree clean" was the only guard, and this repo has many worktrees. Two of them were
# clean, older than production and still had `set-peer` - the command the 2026-09-25 audit
# removed because it let any admin token point a room's microphone anywhere. Deploying from
# either would have silently undone that and ten other control-plane fixes, and the check at the
# end would still have said "matches", because it compares against what was just sent. So:
# production's commit must be an ancestor of HEAD, or the operator must say ROLLBACK=1.
#
# "Production" is what the fleet container on the host was built from - asked over ssh, so it
# is known even while the app is crash-looping and the domain answers 502 - then what the domain
# reports, then the image that `latest` names when no container exists.
LIVE_SHA="${HOST_SHA:-${LIVE_HEALTH:-$LATEST_SHA}}"
LIVE_BASE="${LIVE_SHA%-dirty}"
if [ -z "$LIVE_SHA" ] || [ "$LIVE_SHA" = unknown ]; then
  say "ancestry nothing on the host reports a commit (first deploy?) - not checked"
elif ! git -C "$REPO" cat-file -e "$LIVE_BASE^{commit}" 2>/dev/null; then
  if [ "${ROLLBACK:-}" = 1 ]; then
    err "WARNING: production runs $LIVE_SHA, which this clone does not have - deploying anyway (ROLLBACK=1)."
  else
    err "REFUSING: production runs $LIVE_SHA, a commit this clone does not have, so nothing can say"
    err "          whether this deploy would undo it. Fetch it first (git fetch), or deploy from the"
    err "          clone that made it. Nothing was changed."
    exit 1
  fi
elif ! git -C "$REPO" merge-base --is-ancestor "$LIVE_BASE" HEAD; then
  if [ "${ROLLBACK:-}" = 1 ]; then
    err "WARNING: this commit does not contain production's $LIVE_SHA - rolling back deliberately (ROLLBACK=1)."
  else
    UNDONE="$(git -C "$REPO" log --oneline -n 20 "HEAD..$LIVE_BASE" -- control-plane/ 2>/dev/null || true)"
    err "REFUSING: production runs $LIVE_SHA, and this commit ($GIT_SHA) does not contain it."
    if [ -n "$UNDONE" ]; then
      err "          Deploying would silently undo these control-plane commits:"
      printf '%s\n' "$UNDONE" | sed 's/^/              /' >&2
    else
      err "          (none of the commits it lacks touch control-plane/, but the rule still holds:"
      err "          production's commit must be part of what is deployed)"
    fi
    err "          Deploy from a checkout that contains it (git merge-base --is-ancestor $LIVE_BASE HEAD),"
    err "          put back the build before the last deploy:  $0 --rollback $HOST"
    err "          or, to deploy this older commit on purpose: ROLLBACK=1 $0 $HOST"
    err "          Nothing was changed."
    exit 1
  fi
else
  say "ancestry production's $LIVE_SHA is contained in this commit"
fi
if [ -n "$LIVE_SHA" ] && [ "$LIVE_SHA" != "$LIVE_BASE" ]; then
  err "WARNING: production was deployed from a DIRTY tree ($LIVE_SHA). Its uncommitted changes are in no"
  err "         commit, so nothing can check whether this deploy keeps them."
fi

# ---------------------------------------------------------------------------------------
# BUILD, into a fresh directory and under this commit's own tag. Nothing live is touched: the
# running container keeps its image, netbridge-fleet:latest moves only at the switch below, and
# the source goes into a new directory rather than over the old one, so a file deleted in this
# commit cannot linger in the build.
say "sending source (backend + built panel + Dockerfile)"
tar -C "$CP" -czf - Dockerfile backend/requirements.txt backend/app panel-dist \
  | $S "sudo mkdir -p $R && sudo chown -R ubuntu:ubuntu $R && rm -rf $R/src.new && mkdir -p $R/src.new && tar -C $R/src.new -xzf -"

TAG="netbridge-fleet:$GIT_SHA"
say "building $TAG on the host (nothing live changes unless this succeeds)"
# No `| tail -1` here: the remote shell has no pipefail, so that pipe reported tail's success
# for a build that failed, and the deploy carried on (2026-09-28).
if ! NEW_IMAGE="$($S "cd $R/src.new && sudo docker build -q \
      --build-arg CONTROL_PLANE_GIT_SHA='$GIT_SHA' \
      --build-arg CONTROL_PLANE_BUILD_ID='$BUILD_ID' \
      --build-arg CONTROL_PLANE_BUILT_AT='$BUILT_AT' \
      -t '$TAG' .")" || [ -z "$NEW_IMAGE" ]; then
  err "FAILED: the image did not build (the error is above). Nothing live was changed: the running"
  err "        app, its compose file and the proxy config are exactly as they were."
  exit 1
fi
say "built    $(printf '%s' "$NEW_IMAGE" | tail -1 | cut -c1-19)"

# BACK UP THE DATABASE before anything can migrate it (2026-09-25 adds fleet numbers and
# backfills them at start-up). SQLite's own backup API gives a consistent copy even mid-write - a
# plain cp can catch half a transaction. Kept on the data volume.
#
# Taken inside the running container when there is one. When there is not - a build that crashes
# at import (migrations run there) sits in a restart loop, and `docker exec` refuses a restarting
# container - it is taken by a one-off container of the image just built, on the same volume.
# Until 2026-09-28 that case aborted the deploy, so redeploying the previous commit to recover
# from a crash loop was refused exactly when it was needed.
BACKUP="bridge.db.pre-${GIT_SHA:0:7}-$(date -u +%Y%m%d%H%M%S)"
$S "cat > $R/.db-backup.py" <<'PY'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
if not os.path.exists(src):
    print("[deploy] no database at %s yet - nothing to back up" % src)
    sys.exit(0)
s, d = sqlite3.connect(src), sqlite3.connect(dst)
s.backup(d)
# The copy keeps no PINs: a finished set-pin/unlock never needs its PIN again (review, 2026-09-25).
n = d.execute("update commands set args = ? where type in ('set-pin', 'unlock') and status in "
              "('done', 'succeeded', 'failed', 'rejected', 'cancelled', 'expired') and args like '%pin%'",
              ('{"_scrubbed": true}',)).rowcount
d.commit(); d.close()
print("[deploy] backup ok: %d devices; %d finished PIN row(s) scrubbed in the copy"
      % (s.execute("select count(*) from devices").fetchone()[0], n))
PY
say "backing up the database -> /data/$BACKUP"
if ! $S bash -s -- "$R" "$BACKUP" "$TAG" >"$LT/backup.out" 2>&1 <<'EOF'
cd "$1" && [ -f docker-compose.yml ] || { echo "[deploy] first deploy: no compose project on the host yet - nothing to back up"; exit 0; }
if [ -n "$(sudo docker compose ps -q --status running fleet 2>/dev/null)" ]; then
  sudo docker compose exec -T fleet python - /data/bridge.db "/data/$2" < .db-backup.py
else
  echo "[deploy] the fleet container is not running - backing up its volume with a one-off container of $3"
  printf 'services:\n  fleet:\n    image: "%s"\n' "$3" > .backup-image.yml
  sudo docker compose -f docker-compose.yml -f .backup-image.yml run --rm --no-deps -T fleet \
    python - /data/bridge.db "/data/$2" < .db-backup.py
  rc=$?; rm -f .backup-image.yml; exit $rc
fi
EOF
then
  cat "$LT/backup.out" >&2
  err "REFUSING: could not back up the database - nothing live was changed."
  exit 1
fi
cat "$LT/backup.out"
BACKED_UP=0; grep -q "backup ok" "$LT/backup.out" && BACKED_UP=1

# VALIDATE THE NEW COMPOSE FILE AND CADDYFILE BEFORE EITHER REPLACES THE LIVE ONE. They go up as
# *.new; Caddy checks the new Caddyfile through the new compose file (with the host's .env, so
# the site address is the real one). Until 2026-09-28 the new Caddyfile overwrote the live one
# first and a failed validation was only a warning: the deploy said "done", Caddy kept the old
# config in memory, and the broken file waited on disk for the next reboot to take HTTPS down.
say "sending the compose file and Caddyfile (as .new - not live yet)"
$S "cat > $R/docker-compose.yml.new" < "$HERE/docker-compose.yml"
$S "cat > $R/Caddyfile.new" < "$HERE/Caddyfile"
say "validating the new proxy config"
if ! $S bash -s -- "$R" >"$LT/validate.out" 2>&1 <<'EOF'
cd "$1" && sudo docker compose -f docker-compose.yml.new run --rm --no-deps -T \
  -v "$1/Caddyfile.new:/etc/caddy/Caddyfile.new:ro" caddy \
  caddy validate --config /etc/caddy/Caddyfile.new --adapter caddyfile
EOF
then
  cat "$LT/validate.out" >&2
  $S "rm -f $R/Caddyfile.new $R/docker-compose.yml.new" || true
  err "FAILED: the new Caddyfile or compose file does not validate (above). Nothing live was changed:"
  err "        the proxy keeps its config, and the file on disk is still the one that works."
  exit 1
fi
say "proxy config valid"

# SWITCH. The build being replaced becomes netbridge-fleet:previous (for --rollback), but only
# if it is serving right now - see the rollback section. The Caddyfile is rewritten IN PLACE,
# never renamed over: it is bind-mounted as a single file, which pins the inode, so a renamed-in
# file would stay invisible to the running Caddy and the reload below would re-read the old one.
if [ "$SERVING" = 1 ] && [ -n "$HOST_IMAGE" ]; then
  say "keeping the build being replaced ($HOST_SHA) as netbridge-fleet:previous"
  $S "sudo docker tag '$HOST_IMAGE' netbridge-fleet:previous" \
    || { err "FAILED: could not tag the running build as netbridge-fleet:previous - nothing live was changed."; exit 1; }
elif [ -n "$HOST_IMAGE" ]; then
  say "the running build is not serving, so netbridge-fleet:previous stays on the last one that was"
fi
say "switching to $TAG"
if ! $S bash -s -- "$R" "$TAG" <<'EOF'
set -e
cd "$1"
sudo docker tag "$2" netbridge-fleet:latest
mv docker-compose.yml.new docker-compose.yml
if [ -f Caddyfile ]; then cp -p Caddyfile Caddyfile.prev; fi
cat Caddyfile.new > Caddyfile
rm -f Caddyfile.new
rm -rf src.prev
if [ -d src ]; then mv src src.prev; fi
mv src.new src
EOF
then
  err "FAILED: could not put the new build and files in place on the host (above)."
  host_logs
  exit 1
fi

say "starting"
$S "cd $R && sudo docker compose up -d && sleep 5 && sudo docker compose ps" \
  || { err "FAILED: docker compose up did not complete."; host_logs; exit 1; }

# Caddy reads its Caddyfile once at start (no --watch), and `compose up -d` does not restart it
# for a changed bind-mounted file - so a proxy change (2026-09-25: the panel's live stream must
# pass uncompressed and unbuffered) would be copied and then silently ignored. Reload gracefully:
# no dropped connections. The file already validated, so a refusal here is a failed deploy.
say "reloading the proxy config"
if $S "cd $R && sudo docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile" \
     >"$LT/reload.out" 2>&1; then
  say "proxy config reloaded"
else
  cat "$LT/reload.out" >&2
  $S "cd $R && if [ -f Caddyfile.prev ]; then cat Caddyfile.prev > Caddyfile; fi" || true
  err "FAILED: Caddy would not load the new proxy config (above). The previous Caddyfile is back on"
  err "        disk, so a restart comes up on the config that was serving. Treat this deploy as failed."
  host_logs caddy
  exit 1
fi

verify_live "$GIT_SHA"
DOCS_PUBLIC="$(curl -fsS -m 10 "$D/healthz" 2>/dev/null | sed -n 's/.*"api_docs_public" *: *\([a-z]*\).*/\1/p' || true)"
# The panel is part of what shipped: prove the page being served is the one in this commit, and
# that the admin stream refuses anyone without a credential.
# Retried, and never silent: under `set -e` a failed curl inside $(...) used to end the script with
# no message at all - exactly the kind of "deploy finished?" the checks exist to rule out.
WANT_PANEL="$(shasum -a 256 "$CP/panel-dist/index.html" | cut -c1-16)"
GOT_PANEL="unreachable"
PAGE_FILE="$LT/page.html"
for i in $(seq 1 10); do
  # Hash the bytes as served, from a file: $(...) would strip the page's final newline and the
  # hashes could never match (found in review, 2026-09-25).
  if curl -fsS -m 20 -o "$PAGE_FILE" "$D/" 2>/dev/null; then GOT_PANEL="$(shasum -a 256 "$PAGE_FILE" | cut -c1-16)"; fi
  [ "$GOT_PANEL" = "$WANT_PANEL" ] && break
  sleep 3
done
if [ "$WANT_PANEL" = "$GOT_PANEL" ]; then say "panel        $GOT_PANEL  (matches)"
else err "FAILED: the panel being served ($GOT_PANEL) is not this commit's ($WANT_PANEL)."; exit 1; fi
verify_stream_closed

# Every deploy leaves a netbridge-fleet:<commit> tag, and a tag keeps its image on the host for
# good (`docker image prune` removes only untagged ones), which would fill the small disk one
# build at a time. Keep what latest and previous point at; untag the rest, which deletes each
# image no container is using. Best effort: a live, verified deploy never fails here.
$S bash -s >/dev/null 2>&1 <<'EOF' || true
keep="$(sudo docker image inspect -f '{{.Id}}' netbridge-fleet:latest netbridge-fleet:previous 2>/dev/null)"
sudo docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}' netbridge-fleet |
while read -r ref id; do
  case "$ref" in *:latest|*:previous|*:"<none>") continue ;; esac
  printf '%s\n' "$keep" | grep -qxF "$id" || sudo docker rmi "$ref"
done
EOF

if [ "$BACKED_UP" = 1 ]; then say "database backup kept at /data/$BACKUP (fleetdata volume)"
else say "no database backup was taken (nothing to back up yet)"; fi
say "api docs public: ${DOCS_PUBLIC:-unknown}"
[ "$DOCS_PUBLIC" = "true" ] && say "NOTE: interactive API docs are PUBLIC on this deployment (NB_API_DOCS=1)."
say "done"
