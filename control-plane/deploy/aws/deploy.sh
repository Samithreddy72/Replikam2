#!/bin/bash
# Ship the control plane to the fleet host.
#
# The image is built ON THE HOST, not here. Building locally would need Docker on the Mac
# (it is not installed) and would then stream a ~200 MB image over ssh on every deploy. The
# source is a few hundred KB, and the host already has Docker because provision.sh put it
# there — so we send source and build where it runs.
set -euo pipefail
HOST="${1:?usage: deploy.sh <host-or-ip> [ssh-key]}"
KEY="${2:-$HOME/.ssh/netbridge-fleet.pem}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CP="$(cd "$HERE/../.." && pwd)"                       # control-plane/
S="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$HOST"

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
GIT_SHA="$(git -C "$CP/.." rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git -C "$CP/.." status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILD_ID="${BUILD_ID:-manual-$(date -u +%Y%m%d%H%M%S)}"

if [ "$DIRTY" != "0" ] && [ "${ALLOW_DIRTY:-}" != "1" ]; then
  echo "[deploy] REFUSING: the working tree has $DIRTY uncommitted change(s)." >&2
  echo "         Deploying now would put code in production that matches no commit, and" >&2
  echo "         nothing afterwards could tell you what is running." >&2
  echo "         Commit first, or re-run with ALLOW_DIRTY=1 to deploy deliberately." >&2
  exit 1
fi
[ "$DIRTY" != "0" ] && GIT_SHA="${GIT_SHA}-dirty"

echo "[deploy] commit   $GIT_SHA"
echo "[deploy] build id $BUILD_ID"

# BACK UP THE DATABASE FIRST. A new build may migrate it (2026-09-25 adds fleet numbers and
# backfills them). SQLite's own backup API, run inside the RUNNING container, gives a consistent
# copy even mid-write - a plain cp can catch half a transaction. Kept on the data volume, so the
# rollback is: redeploy the previous commit and, only if needed, copy this file back.
BACKUP="bridge.db.pre-${GIT_SHA:0:7}-$(date -u +%Y%m%d%H%M%S)"
echo "[deploy] backing up the database -> /data/$BACKUP"
# The script travels on stdin (ssh -> docker exec -T -> python -), so no quoting can mangle it.
$S "cd /opt/netbridge && sudo docker compose exec -T fleet python - /data/bridge.db /data/$BACKUP" <<'PY' \
  || { echo "[deploy] REFUSING: could not back up the database - nothing was changed." >&2; exit 1; }
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
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

echo "[deploy] sending source (backend + built panel + Dockerfile)"
tar -C "$CP" -czf - Dockerfile backend/requirements.txt backend/app panel-dist \
  | $S 'sudo mkdir -p /opt/netbridge/src && sudo chown -R ubuntu:ubuntu /opt/netbridge && tar -C /opt/netbridge/src -xzf -'

echo "[deploy] sending compose files"
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new \
    "$HERE/docker-compose.yml" "$HERE/Caddyfile" "ubuntu@$HOST:/opt/netbridge/"

echo "[deploy] building on the host"
$S "cd /opt/netbridge/src && sudo docker build -q \
      --build-arg CONTROL_PLANE_GIT_SHA='$GIT_SHA' \
      --build-arg CONTROL_PLANE_BUILD_ID='$BUILD_ID' \
      --build-arg CONTROL_PLANE_BUILT_AT='$BUILT_AT' \
      -t netbridge-fleet:latest . | tail -1"

echo "[deploy] starting"
$S 'cd /opt/netbridge && sudo docker compose up -d && sleep 5 && sudo docker compose ps'

# Caddy reads its Caddyfile once at start (no --watch), and `compose up -d` does not restart it
# for a changed bind-mounted file - so a proxy change (2026-09-25: the panel's live stream must
# pass uncompressed and unbuffered) would be copied and then silently ignored. Validate, then
# reload gracefully: no dropped connections, and an invalid file leaves the old config serving.
echo "[deploy] reloading the proxy config"
$S 'cd /opt/netbridge && sudo docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
     && sudo docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile' \
  && echo "[deploy] proxy config reloaded" \
  || { echo "[deploy] WARNING: the new Caddyfile did not validate/reload - the OLD proxy config is still serving." >&2; }

# ---------------------------------------------------------------------------------------
# PROVE IT. `docker compose ps` says a container is up, which is the same class of green
# light as `{"ok": true}` -- it does not say the NEW code is serving. Ask the live server
# what it is, and fail if it is not what we just sent.
echo "[deploy] verifying what is actually live"
for i in $(seq 1 20); do
  LIVE="$(curl -fsS -m 10 "https://${DOMAIN:-fleet.scine.online}/healthz" 2>/dev/null || true)"
  echo "$LIVE" | grep -q '"ok"' && break
  sleep 3
done
LIVE_SHA="$(printf '%s' "$LIVE" | sed -n 's/.*"git_sha" *: *"\([^"]*\)".*/\1/p')"
if [ -z "$LIVE_SHA" ]; then
  echo "[deploy] WARNING: the live server did not report a git_sha. Either it is running a" >&2
  echo "         build from before this identity work, or it is not the server we just" >&2
  echo "         deployed to. Do NOT record this deploy as verified." >&2
  exit 1
fi
if [ "$LIVE_SHA" != "$GIT_SHA" ]; then
  echo "[deploy] FAILED: live server reports $LIVE_SHA but we deployed $GIT_SHA." >&2
  echo "         The deploy did not take effect. Investigate before assuming it worked." >&2
  exit 1
fi
DOCS_PUBLIC="$(printf '%s' "$LIVE" | sed -n 's/.*"api_docs_public" *: *\([a-z]*\).*/\1/p')"
echo "[deploy] live commit  $LIVE_SHA  (matches)"
# The panel is part of what shipped: prove the page being served is the one in this commit, and
# that the admin stream refuses anyone without a credential.
D="https://${DOMAIN:-fleet.scine.online}"
# Retried, and never silent: under `set -e` a failed curl inside $(...) used to end the script with
# no message at all - exactly the kind of "deploy finished?" the checks exist to rule out.
WANT_PANEL="$(shasum -a 256 "$CP/panel-dist/index.html" | cut -c1-16)"
GOT_PANEL="unreachable"
PAGE_FILE="$(mktemp)"
for i in $(seq 1 10); do
  # Hash the bytes as served, from a file: $(...) would strip the page's final newline and the
  # hashes could never match (found in review, 2026-09-25).
  if curl -fsS -m 20 -o "$PAGE_FILE" "$D/" 2>/dev/null; then GOT_PANEL="$(shasum -a 256 "$PAGE_FILE" | cut -c1-16)"; fi
  [ "$GOT_PANEL" = "$WANT_PANEL" ] && break
  sleep 3
done
rm -f "$PAGE_FILE"
if [ "$WANT_PANEL" = "$GOT_PANEL" ]; then echo "[deploy] panel        $GOT_PANEL  (matches)"
else echo "[deploy] FAILED: the panel being served ($GOT_PANEL) is not this commit's ($WANT_PANEL)." >&2; exit 1; fi
STREAM_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$D/admin/stream" || true)"
if [ "$STREAM_CODE" = "401" ]; then echo "[deploy] /admin/stream refuses anonymous callers (401)"
else echo "[deploy] FAILED: /admin/stream answered '$STREAM_CODE' to an anonymous caller (want 401)." >&2; exit 1; fi
echo "[deploy] database backup kept at /data/$BACKUP (fleetdata volume)"
echo "[deploy] api docs public: ${DOCS_PUBLIC:-unknown}"
[ "$DOCS_PUBLIC" = "true" ] && echo "[deploy] NOTE: interactive API docs are PUBLIC on this deployment (NB_API_DOCS=1)."
echo "[deploy] done"
