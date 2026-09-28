#!/bin/bash
# tools/publish-ota.sh and the fleet's OS-version catalog, run for real (2026-09-28).
#
# publish-ota.sh copied a 1.1 GB image straight into the directory the fleet serves, so for the
# minutes the copy took the panel offered "Install on..." for a version whose image was missing
# or half written; free space was only printed; and nothing ever removed an old version from
# the disk that also holds bridge.db. This runs the REAL script and the REAL fleet server
# (uvicorn on a random local port, a throwaway SQLite database and payload directory) with
# stand-ins only for what needs the outside world: GitHub (gh, and curl to api.github.com), the
# fleet host (ssh, scp, sudo, docker, df). Nothing leaves this machine: the stand-in curl
# refuses any host but 127.0.0.1, and the stand-in ssh refuses any command that names a real
# path instead of the sandbox. --prune's question is answered on a real pseudo-terminal, and
# rollouts go straight into the throwaway database. Skipped, and says so, without FastAPI.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"; SRV=""
trap '[ -z "$SRV" ] || kill "$SRV" 2>/dev/null; rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); sed 's/^/        /' "$T/out" 2>/dev/null | tail -5; }

PY=""
for c in "${FLEET_TEST_PY:-}" "$HOME/netbridge/fleet-test-venv/bin/python" /private/tmp/claude-501/bev311/bin/python python3; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && "$c" -c 'import fastapi, uvicorn, sqlalchemy' 2>/dev/null && { PY="$c"; break; }
done
# "SKIPPED" is the word tools/run-tests.sh counts as a skip (rather than a crash).
[ -n "$PY" ] || { echo "  SKIPPED  FastAPI not installed (set FLEET_TEST_PY or make ~/netbridge/fleet-test-venv)"; exit 0; }
REAL_CURL="$(command -v curl)"

# Not under a "data/payloads" path, so the stand-in ssh can tell a sandbox path from a real one.
OTA="$T/c/payloads/ota"; HT="$T/hosttmp"
mkdir -p "$T/bin" "$T/home/.netbridge/keys" "$T/gh/blobs" "$OTA" "$HT"
openssl ecparam -name prime256v1 -genkey -noout -out "$T/ota-key.pem" 2>/dev/null
openssl ec -in "$T/ota-key.pem" -pubout -out "$T/home/.netbridge/keys/ota-pubkey.pem" 2>/dev/null
echo "test-admin-key" > "$T/token"; echo "wrong-key" > "$T/bad-token"; : > "$T/fleet.pem"
echo 50000 > "$T/df_free"

# ---- the fleet server --------------------------------------------------------------------------
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
FLEET="http://127.0.0.1:$PORT"
( cd "$REPO/control-plane/backend" && exec env -i PATH="$PATH" HOME="$T/home" \
    DATABASE_URL="sqlite:///$T/fleet.db" PAYLOAD_DIR="$T/c/payloads" ADMIN_API_KEY="test-admin-key" \
    ALERT_EVAL_INTERVAL_S=3600 OFFLINE_AFTER_S=60 \
    "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning ) >"$T/server.log" 2>&1 &
SRV=$!
for _ in $(seq 1 150); do "$REAL_CURL" -fsS -m 1 "$FLEET/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
"$REAL_CURL" -fsS -m 2 "$FLEET/healthz" >/dev/null 2>&1 || { echo "  FAIL  fleet server did not start"; cat "$T/server.log"; exit 1; }

# ---- stand-ins -------------------------------------------------------------------------------
cat > "$T/bin/gh" <<EOF
#!/bin/bash
[ "\$1 \$2" = "auth token" ] && { echo gho_TESTTOKEN_not_real; exit 0; }
[ "\$1" = api ] || exit 1
shift
if [ "\$1" = -H ]; then cat "$T/gh/blobs/\${3##*/}"; exit; fi
tag=\$(printf '%s' "\$*" | sed -n 's/.*tag_name=="\([^"]*\)".*/\1/p')
cat "$T/gh/\$tag/assets" 2>/dev/null; exit 0
EOF
cat > "$T/bin/curl" <<EOF
#!/bin/bash
gh=""
for a; do case "\$a" in
  https://api.github.com/*) gh="\$a" ;;
  http://127.0.0.1:$PORT/*) ;;
  http://*|https://*) echo "\$a" >> "$T/refused"; echo "test curl: refusing \$a" >&2; exit 7 ;;
esac; done
[ -n "\$gh" ] || exec "$REAL_CURL" "\$@"
out=""; auth=""; prev=""
for a; do [ "\$prev" = -o ] && out="\$a"; [ "\$prev" = -K ] && auth="\$a"; prev="\$a"; done
grep -q gho_TESTTOKEN_not_real "\$auth" || { echo "no GitHub token in the -K file" >&2; exit 22; }
id="\${gh##*/}"
if [ -e "$T/gh_fail" ] && [ "\$(cat "$T/gh_fail")" = "\$id" ]; then head -c 1000 "$T/gh/blobs/\$id" > "\$out"; exit 18; fi
cp "$T/gh/blobs/\$id" "\$out"
EOF
cat > "$T/bin/ssh" <<EOF
#!/bin/bash
for a; do cmd="\$a"; done
n=\$(( \$(cat "$T/ssh.n" 2>/dev/null || echo 0) + 1 )); echo \$n > "$T/ssh.n"
# stdin carries the script for "sh -s" / "bash -s". On a terminal (while --prune asks its
# question) nothing is piped in; the real ssh would only forward keystrokes nobody reads.
if [ -t 0 ]; then : > "$T/ssh.in.\$n"; else cat > "$T/ssh.in.\$n"; fi
printf '%s\n' "\$cmd" >> "$T/ssh.log"
chk="\$(printf '%s\n' "\$cmd"; cat "$T/ssh.in.\$n")"; chk="\${chk//"$T"/}"
case "\$chk" in *"/data/payloads"*|*"/tmp/ota-"*) echo "\$cmd" >> "$T/unsandboxed"; exit 99 ;; esac
bash -c "\$cmd" < "$T/ssh.in.\$n"
EOF
cat > "$T/bin/scp" <<'EOF'
#!/bin/bash
files=()
while [ $# -gt 0 ]; do case "$1" in -i|-o) shift 2 ;; *:*) dest="${1#*:}"; shift ;; *) files+=("$1"); shift ;; esac; done
cp "${files[@]}" "$dest"
EOF
printf '#!/bin/bash\nexec "$@"\n' > "$T/bin/sudo"
cat > "$T/bin/docker" <<EOF
#!/bin/bash
[ "\$1" = compose ] || exit 1; shift
[ "\$1" = -f ] && shift 2
case "\$1" in
  exec) shift; [ "\$1" = -T ] && shift; [ "\$1" = fleet ] || exit 1; shift; exec "\$@" ;;
  cp) src="\${2%/.}"; dst="\${3#fleet:}"; v="\$(basename "\$src")"; v="\${v#ota-}"
      [ -e "$T/cp_fail" ] && exit 1
      mkdir -p "\$dst" && cp "\$src/manifest.txt" "\$src/manifest.txt.sig" "\$dst/" || exit 1
      # What a bridge and the panel can see WHILE the image is still being copied.
      ls -A "$OTA" > "$T/during.ls"
      "$REAL_CURL" -s -m 5 -H "Authorization: Bearer test-admin-key" "$FLEET/admin/payloads/ota" > "$T/during.json"
      "$REAL_CURL" -s -m 5 -o /dev/null -w '%{http_code}' "$FLEET/payloads/ota/\$v/manifest.txt" > "$T/during.http"
      if [ -e "$T/cp_truncate" ]; then head -c 1000 "\$src/rootfs.tar.zst" > "\$dst/rootfs.tar.zst"
      else cp "\$src/rootfs.tar.zst" "\$dst/"; fi ;;
  *) exit 1 ;;
esac
EOF
cat > "$T/bin/df" <<EOF
#!/bin/bash
echo "Filesystem 1048576-blocks Used Available Capacity Mounted on"
echo "/dev/root 80000 10000 \$(cat "$T/df_free") 12% /"
EOF
printf '#!/bin/bash\nshasum -a 256 "$@"\n' > "$T/bin/sha256sum"
chmod +x "$T"/bin/*

ID=100
mkrel(){ # mkrel <version> [image bytes]: a signed release, as CI publishes it
  local v="$1" n="${2:-300000}" d="$T/gh/v$1" f
  mkdir -p "$d"; head -c "$n" /dev/urandom > "$d/rootfs.tar.zst"
  printf 'product=netbridge-os\nversion=%s\nimage=rootfs.tar.zst\nsha256=%s\nsize=%s\nkernel=6.6.51\nbuilt=2026-09-28T00:00:00Z\n' \
    "$v" "$(shasum -a 256 "$d/rootfs.tar.zst" | cut -d' ' -f1)" "$n" > "$d/manifest.txt"
  openssl dgst -sha256 -sign "$T/ota-key.pem" -out "$d/manifest.txt.sig" "$d/manifest.txt"
  for f in manifest.txt manifest.txt.sig rootfs.tar.zst; do
    ID=$((ID+1)); cp "$d/$f" "$T/gh/blobs/$ID"; echo "$f $ID $(wc -c < "$d/$f" | tr -d ' ')" >> "$d/assets"
  done; }
OTA_ENV=(HOME="$T/home" PATH="$T/bin:$PATH" FLEET_URL="$FLEET" FLEET_HOST=test@fleet.invalid FLEET_SSH_KEY="$T/fleet.pem"
         FLEET_COMPOSE=/opt/netbridge/docker-compose.yml FLEET_OTA_DIR="$OTA" FLEET_TMP="$HT" REPO_SLUG=test/netbridge)
ota(){ # ota <publish-ota args...>
  env "${OTA_ENV[@]}" FLEET_MIN_FREE_MB="${MINFREE:-1024}" FLEET_TOKEN_FILE="${TOKEN:-$T/token}" \
      bash "$REPO/tools/publish-ota.sh" "$@" </dev/null >"$T/out" 2>&1
  echo $? > "$T/rc"; }
# --prune asks before it removes anything, but only on a terminal. on-a-terminal.py runs the
# script on a pseudo-terminal, waits for the question, can start a rollout (straight into the
# throwaway database) while the question is on screen, then answers it. A script that does not
# finish within a minute is killed, so the suite can never hang here.
cat > "$T/on-a-terminal.py" <<'PY'
import os, pty, select, sqlite3, sys, time
answer, rollout_for, db, cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
out = b""
def pump(until=None, timeout=60):
    global out
    end = time.time() + timeout
    while time.time() < end:
        if until is not None and until in out:
            return True
        if not select.select([fd], [], [], 0.5)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:                   # the script exited and closed the terminal
            chunk = b""
        if not chunk:
            return until is None
        out += chunk
    return False
if pump(b"[y/N]"):
    if rollout_for:
        c = sqlite3.connect(db)
        c.execute("INSERT INTO rollouts (org_id, status, version, source, stage_pct, created_by, created_at, updated_at) "
                  "VALUES ('default', 'paused', ?, 'test', 10, 'test', '2026-09-28 00:00:00', '2026-09-28 00:00:00')",
                  (rollout_for,))
        c.commit()
        c.close()
    os.write(fd, answer.encode() + b"\n")
pump(timeout=60)
end = time.time() + 10                    # the terminal closes a moment before the exit is reaped
done, status = os.waitpid(pid, os.WNOHANG)
while not done and time.time() < end:
    time.sleep(0.05)
    done, status = os.waitpid(pid, os.WNOHANG)
if not done:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    out += b"\non-a-terminal.py: the script did not finish within a minute - killed\n"
    status = 124 << 8
sys.stdout.write(out.decode(errors="replace").replace("\r\n", "\n"))
sys.exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)
PY
ota_tty(){ # ota_tty <answer> <version a rollout starts using while asked, or ""> <publish-ota args...>
  local answer="$1" ro="$2"; shift 2
  env "${OTA_ENV[@]}" FLEET_MIN_FREE_MB=1024 FLEET_TOKEN_FILE="$T/token" \
      python3 "$T/on-a-terminal.py" "$answer" "$ro" "$T/fleet.db" bash "$REPO/tools/publish-ota.sh" "$@" </dev/null >"$T/out" 2>&1
  echo $? > "$T/rc"; }
rollout(){ # rollout <status> <version> <source>: straight into the throwaway test database
  python3 - "$T/fleet.db" "$@" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO rollouts (org_id, status, version, source, stage_pct, created_by, created_at, updated_at) "
          "VALUES ('default', ?, ?, ?, 10, 'test', '2026-09-28 00:00:00', '2026-09-28 00:00:00')", tuple(sys.argv[2:5]))
c.commit()
PY
}
rc(){ cat "$T/rc"; }
# ago <hours> <path...>: last changed that long ago. Relative to now, because "untouched for 6 hours"
# is measured against the real clock.
ago(){ local h="$1"; shift; touch -t "$(python3 -c 'import sys, time; print(time.strftime("%Y%m%d%H%M", time.localtime(time.time() - float(sys.argv[1]) * 3600)))' "$h")" "$@"; }
offered(){ "$REAL_CURL" -fsS -m 5 -H "Authorization: Bearer test-admin-key" "$FLEET/admin/payloads/ota" \
             | python3 -c 'import json,sys; print(" ".join(x["version"] for x in json.load(sys.stdin) if x["signed"]))'; }
leftovers(){ ls -A "$OTA" | grep -E '^\.(incoming|old)-' ; ls -A "$HT"; }

A=2.2.0-aaaaaaa B=2.2.0-bbbbbbb C=2.2.0-ccccccc D=2.2.0-ddddddd
for v in "$A" "$B" "$C" "$D" 2.1.0-1111111 2.1.0-2222222 2.1.0-3333333 2.1.0-4444444; do mkrel "$v"; done

echo "== the catalog offers only what a bridge can install =="
g="$T/gh/v"
mkdir -p "$OTA/2.1.0-1111111" "$OTA/2.1.0-2222222" "$OTA/2.1.0-3333333" "$OTA/.incoming-2.1.0-4444444" "$OTA/2.1.0-5555555"
cp "$g"2.1.0-1111111/{manifest.txt,manifest.txt.sig,rootfs.tar.zst} "$OTA/2.1.0-1111111/"
cp "$g"2.1.0-2222222/{manifest.txt,manifest.txt.sig} "$OTA/2.1.0-2222222/"                  # image not there yet
cp "$g"2.1.0-3333333/{manifest.txt,manifest.txt.sig} "$OTA/2.1.0-3333333/"
head -c 5000 "$g"2.1.0-3333333/rootfs.tar.zst > "$OTA/2.1.0-3333333/rootfs.tar.zst"           # half copied
cp "$g"2.1.0-4444444/{manifest.txt,manifest.txt.sig,rootfs.tar.zst} "$OTA/.incoming-2.1.0-4444444/"
cp "$g"2.1.0-1111111/{manifest.txt,manifest.txt.sig,rootfs.tar.zst} "$OTA/2.1.0-5555555/"     # manifest is for another version
got="$(offered)"
[ "$got" = "2.1.0-1111111" ] && ok "only the complete version is offered (not: no image, half an image, staging, wrong manifest)" \
  || no "only the complete version is offered — got: $got"
"$REAL_CURL" -fsS -m 5 -H "Authorization: Bearer test-admin-key" "$FLEET/admin/payloads/ota" \
  | python3 -c 'import json,sys; x=json.load(sys.stdin)[0]; sys.exit(0 if x["bytes"] == 300000 and x["image"] == "rootfs.tar.zst" else 1)' \
  && ok "it reports the image's real size" || no "it reports the image's real size"
code="$("$REAL_CURL" -s -o /dev/null -w '%{http_code}' -m 5 "$FLEET/admin/payloads/ota")"
[ "$code" = 401 ] && ok "the catalog is admin-only" || no "the catalog is admin-only (HTTP $code)"
rm -rf "$OTA"/2.1.0-* "$OTA"/.incoming-2.1.0-*

echo "== free space is enforced, not just printed =="
echo 1000 > "$T/df_free"
ota "$A"
{ [ "$(rc)" != 0 ] && grep -q "not enough space" "$T/out" && ! grep -q docker "$T/ssh.log" && [ -z "$(leftovers)" ] && [ ! -e "$OTA/$A" ]; } \
  && ok "a publish that would leave under 1024 MB free is refused before anything is copied" \
  || no "a publish that would leave under 1024 MB free is refused before anything is copied (rc $(rc))"
echo 50000 > "$T/df_free"
MINFREE=lots ota "$A"
{ [ "$(rc)" != 0 ] && grep -q "whole number of MB" "$T/out" && [ ! -e "$OTA/$A" ]; } \
  && ok "a FLEET_MIN_FREE_MB that is not a number is refused, not read as 0" \
  || no "a FLEET_MIN_FREE_MB that is not a number is refused, not read as 0 (rc $(rc))"

echo "== publishing: staged, then renamed in one step =="
: > "$T/ssh.log"
ota "$A"
[ "$(rc)" = 0 ] && ok "$A published (the fleet server downloads it)" || no "$A published (rc $(rc))"
[ "$(cat "$T/during.http" 2>/dev/null)" = 404 ] && ok "while the image was copying, the version was not at its public path" \
  || no "while the image was copying, the version was not at its public path (HTTP $(cat "$T/during.http" 2>/dev/null))"
grep -qx ".incoming-$A" "$T/during.ls" 2>/dev/null && ! grep -qx "$A" "$T/during.ls" \
  && ok "it was copied into .incoming-$A" || no "it was copied into .incoming-$A ($(tr '\n' ' ' < "$T/during.ls" 2>/dev/null))"
python3 -c 'import json,sys; sys.exit(0 if sys.argv[1] not in [x["version"] for x in json.load(open(sys.argv[2]))] else 1)' "$A" "$T/during.json" 2>/dev/null \
  && ok "and the panel did not offer it yet" || no "and the panel did not offer it yet"
[ "$(offered)" = "$A" ] && ok "once complete, it is offered" || no "once complete, it is offered — got: $(offered)"
"$REAL_CURL" -fsS -m 5 "$FLEET/payloads/ota/$A/rootfs.tar.zst" | cmp -s - "$g$A/rootfs.tar.zst" \
  && ok "the fleet serves the exact image" || no "the fleet serves the exact image"
[ -z "$(leftovers)" ] && ok "no staging or host copy left behind" || no "no staging or host copy left behind: $(leftovers | tr '\n' ' ')"
! grep -q gho_TESTTOKEN_not_real "$T/ssh.log" && grep -q gho_TESTTOKEN_not_real "$T"/ssh.in.* \
  && ok "the GitHub token went over ssh stdin, never on a command line" || no "the GitHub token went over ssh stdin, never on a command line"

ota "$B" --via-mac
{ [ "$(rc)" = 0 ] && [ "$(offered)" = "$A $B" ] && [ -z "$(leftovers)" ]; } && ok "$B published --via-mac" || no "$B published --via-mac (rc $(rc))"

echo "== a failed publish publishes nothing and cleans up =="
: > "$T/cp_truncate"; ota "$C"; rm -f "$T/cp_truncate"
{ [ "$(rc)" != 0 ] && grep -q "incomplete" "$T/out" && [ ! -e "$OTA/$C" ] && [ -z "$(leftovers)" ] && [ "$(offered)" = "$A $B" ]; } \
  && ok "a short copy in the container is never renamed into place" || no "a short copy in the container is never renamed into place (rc $(rc))"
: > "$T/cp_fail"; ota "$C"; rm -f "$T/cp_fail"
{ [ "$(rc)" != 0 ] && [ ! -e "$OTA/$C" ] && [ -z "$(leftovers)" ]; } \
  && ok "a failed copy into the container leaves nothing behind" || no "a failed copy into the container leaves nothing behind (rc $(rc))"
awk '$1=="rootfs.tar.zst" {print $2}' "$g$C/assets" > "$T/gh_fail"; ota "$C"; rm -f "$T/gh_fail"
{ [ "$(rc)" != 0 ] && grep -q "server-side download failed" "$T/out" && [ ! -e "$HT/ota-$C" ] && [ ! -e "$OTA/$C" ]; } \
  && ok "an interrupted server-side download removes its partial image from the host" \
  || no "an interrupted server-side download removes its partial image from the host (rc $(rc); $(ls "$HT"))"

ota "$A"
{ [ "$(rc)" = 0 ] && [ "$(offered)" = "$A $B" ] && [ -z "$(leftovers)" ]; } && ok "publishing a version again replaces it cleanly" \
  || no "publishing a version again replaces it cleanly (rc $(rc))"

echo "== --prune keeps the newest N installable versions, never one a rollout uses =="
S=2.1.0-2222222 E=2.2.0-eeeeeee F=2.2.0-fffffff
ota "$C"; [ "$(rc)" = 0 ] || no "$C published (rc $(rc))"
ota "$S"; [ "$(rc)" = 0 ] || no "$S published (rc $(rc))"
# What an interrupted old-style publish left behind: a version directory with no image, newer
# than every good version and untouched for days (E) - and one that changed minutes ago (F).
mkdir -p "$OTA/$E" "$OTA/$F"
printf 'version=%s\nimage=rootfs.tar.zst\n' "$E" > "$OTA/$E/manifest.txt"
printf 'version=%s\nimage=rootfs.tar.zst\n' "$F" > "$OTA/$F/manifest.txt"
ago 648 "$OTA/$A"; ago 552 "$OTA/$S"; ago 432 "$OTA/$B"; ago 192 "$OTA/$C"; ago 72 "$OTA/$E"   # F (now) is the newest
mkdir -p "$OTA/.incoming-2.2.0-9999999" "$OTA/.incoming-2.2.0-8888888" "$HT/ota-2.2.0-7777777" "$HT/ota-2.2.0-6666666"
ago 648 "$OTA/.incoming-2.2.0-9999999" "$HT/ota-2.2.0-7777777"   # a publish killed weeks ago
rollout paused "$A" "$FLEET/payloads/ota/$A"
rollout active 2.9.9-0000000 "$FLEET/payloads/ota/$S"       # made through the API: version and source disagree
ota --prune 1
{ [ "$(rc)" != 0 ] && grep -q "without confirmation" "$T/out" && [ -d "$OTA/$B" ]; } \
  && ok "without --yes (and no terminal to ask on) nothing is removed" || no "without --yes (and no terminal to ask on) nothing is removed (rc $(rc))"
TOKEN="$T/bad-token" ota --prune 1 --yes
{ [ "$(rc)" != 0 ] && grep -q "nothing removed" "$T/out" && [ -d "$OTA/$B" ] && [ -d "$OTA/$E" ]; } \
  && ok "if the fleet cannot say what it offers or which versions rollouts use, nothing is removed" \
  || no "if the fleet cannot say what it offers or which versions rollouts use, nothing is removed (rc $(rc))"
ota --prune 0
[ "$(rc)" != 0 ] && ok "--prune 0 is refused" || no "--prune 0 is refused"
ota --prune 1 --yes
{ [ "$(rc)" = 0 ] && [ -d "$OTA/$C" ] && [ ! -e "$OTA/$B" ]; } \
  && ok "--prune 1 kept the newest installable version ($C) and removed an older one ($B)" \
  || no "--prune 1 kept the newest installable version ($C) and removed an older one ($B) (rc $(rc))"
{ [ ! -e "$OTA/$E" ] && [ -d "$OTA/$F" ] && grep -q "leaving $F alone" "$T/out"; } \
  && ok "an image-less leftover newer than $C took no place and was removed; one that changed minutes ago stayed" \
  || no "an image-less leftover newer than $C took no place and was removed; one that changed minutes ago stayed"
{ [ -d "$OTA/$A" ] && grep -q "keeping $A (an unfinished rollout" "$T/out"; } \
  && ok "the version a paused rollout uses ($A) stays" || no "the version a paused rollout uses ($A) stays"
{ [ -d "$OTA/$S" ] && grep -q "keeping $S (an unfinished rollout" "$T/out"; } \
  && ok "so does the one a rollout's update commands fetch ($S), under whatever version name it was started" \
  || no "so does the one a rollout's update commands fetch ($S), under whatever version name it was started"
{ [ ! -e "$OTA/.incoming-2.2.0-9999999" ] && [ -d "$OTA/.incoming-2.2.0-8888888" ]; } \
  && ok "stale staging went; a fresh one (maybe a publish still running) stayed" || no "stale staging went; a fresh one stayed"
{ [ ! -e "$HT/ota-2.2.0-7777777" ] && [ -d "$HT/ota-2.2.0-6666666" ]; } \
  && ok "so did a killed publish's download in /tmp on the host; a fresh one stayed" \
  || no "so did a killed publish's download in /tmp on the host; a fresh one stayed ($(ls "$HT" | tr '\n' ' '))"
[ "$(offered)" = "$S $A $C" ] && ok "the catalog agrees" || no "the catalog agrees — got: $(offered)"
rm -rf "$OTA/.incoming-2.2.0-8888888" "$HT/ota-2.2.0-6666666"
ota "$D" --prune 1 --yes
{ [ "$(rc)" = 0 ] && [ "$(offered)" = "$S $A $D" ]; } && ok "publish $D --prune 1: $D in, $C out, the rollouts' versions still there" \
  || no "publish $D --prune 1: $D in, $C out, the rollouts' versions still there (rc $(rc); offered: $(offered))"

echo "== --prune asks first, and checks the rollouts again after the answer =="
ota "$B"; ota "$C"                                            # C is the newest; D and B are old now
ago 48 "$OTA/$D"; ago 96 "$OTA/$B"
ota_tty n "" --prune 1
{ [ "$(rc)" != 0 ] && grep -q "Remove these 2 version(s)" "$T/out" && grep -q "nothing removed" "$T/out" && [ -d "$OTA/$D" ] && [ -d "$OTA/$B" ]; } \
  && ok "answering no removes nothing" || no "answering no removes nothing (rc $(rc))"
ota_tty y "$D" --prune 1
{ [ "$(rc)" = 0 ] && grep -q "keeping $D (a rollout started using it)" "$T/out" && [ -d "$OTA/$D" ] && [ ! -e "$OTA/$B" ]; } \
  && ok "a rollout started while the question was on screen keeps its version ($D); yes removed the other ($B)" \
  || no "a rollout started while the question was on screen keeps its version ($D); yes removed the other ($B) (rc $(rc))"
rm -rf "$OTA/$F"
ota --list
{ [ "$(rc)" = 0 ] && grep -q "\"$D\"" "$T/out"; } && ok "--list shows what the fleet offers" || no "--list shows what the fleet offers"

echo "== nothing left the sandbox =="
[ ! -e "$T/unsandboxed" ] && ok "no command on the stand-in fleet host named a real path" || { : > "$T/out"; cp "$T/unsandboxed" "$T/out"; no "no command on the stand-in fleet host named a real path"; }
[ ! -e "$T/refused" ] && ok "no request went anywhere but the local test server" || { cp "$T/refused" "$T/out"; no "no request went anywhere but the local test server"; }

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
