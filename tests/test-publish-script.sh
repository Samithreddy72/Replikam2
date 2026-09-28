#!/bin/bash
# tools/publish-script.sh, run for real (2026-09-28). No test ran it before, and its Python check
# refused EVERY Python file (py_compile to /dev/null raises before compiling anything), so
# `nb deploy <bridge> x.py` could never work for the nine Python files in the catalog. A --name
# with nothing after it also spun forever, so every run here is killed after 20 s ("hung").
#
# Part 1 needs nothing: the checks the bridge would run are made before anything is uploaded,
# so a good file gets as far as the upload (exit 5 against a closed port) and a bad one is
# refused first (exit 3). Part 2 publishes to the REAL fleet server (uvicorn on a random local
# port, a throwaway SQLite database and payload directory) and verifies what it serves with the
# public key, exactly as a bridge does at install. Part 2 is skipped, and says so, without FastAPI.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"; SRV=""
trap '[ -z "$SRV" ] || kill "$SRV" 2>/dev/null; rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); sed 's/^/        /' "$T/out" 2>/dev/null | tail -4; }

# A sandbox HOME: a throwaway signing key, its public half, and a fleet token.
K="$T/home/.netbridge/keys"; mkdir -p "$K" "$T/src" "$T/bad"
openssl ecparam -name prime256v1 -genkey -noout -out "$K/script-signing-key.pem" 2>/dev/null
openssl ec -in "$K/script-signing-key.pem" -pubout -out "$K/script-pubkey.pem" 2>/dev/null
echo "test-admin-key" > "$T/home/.netbridge/fleet-automation-token"

pub(){ # pub <fleet-url> <publish-script args...>; a run still going after 20 s is killed: rc "hung"
  local url="$1" p i; shift
  env HOME="$T/home" FLEET_URL="$url" FLEET_TOKEN_FILE="$T/home/.netbridge/fleet-automation-token" \
    bash "$REPO/tools/publish-script.sh" "$@" </dev/null >"$T/out" 2>&1 &
  p=$!
  for i in $(seq 1 200); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done
  if kill -0 "$p" 2>/dev/null; then kill "$p"; wait "$p" 2>/dev/null; echo hung > "$T/rc"
  else wait "$p"; echo $? > "$T/rc"; fi; }
rc(){ cat "$T/rc"; }
free_port(){ python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }

# One of each kind in the catalog, all of them real files where the repo has one.
cp "$REPO/pi/scripts/bridge-web.py" "$REPO/pi/scripts/bridge-agent.py" "$REPO/pi/scripts/bridge-status.sh" "$T/src/"
ssh-keygen -q -t ed25519 -N '' -C test-owner -f "$T/owner" >/dev/null
cp "$T/owner.pub" "$T/src/owner_ssh_authorized_keys"
printf '[Service]\nCPUQuota=90%%\n' > "$T/src/video.conf"
# ... and the broken versions each check exists to catch.
printf '#!/usr/bin/env python3\ndef status(:\n    return 1\n' > "$T/bad/bridge-read.py"
printf '#!/bin/bash\nif then fi\n' > "$T/bad/bridge-wait.sh"
printf '# every key removed\n\n' > "$T/bad/owner_ssh_authorized_keys"
printf '[Timer]\nOnBootSec=1\n' > "$T/bad/timer.conf"
printf '#!/bin/bash\necho hi\n' > "$T/bad/not-in-catalog.sh"

echo "== part 1: the checks before upload =="
CLOSED="http://127.0.0.1:$(free_port)"
for f in bridge-web.py bridge-agent.py; do
  pub "$CLOSED" "$T/src/$f"
  { [ "$(rc)" = 5 ] && grep -q "signed  $f (bind)" "$T/out" && ! grep -q "does not compile" "$T/out"; } \
    && ok "valid Python ($f) passes the compile check and is signed" || no "valid Python ($f) passes the compile check and is signed (rc $(rc))"
done
pub "$CLOSED" "$T/src/bridge-status.sh"
[ "$(rc)" = 5 ] && ok "a valid bash script passes the syntax check" || no "a valid bash script passes the syntax check (rc $(rc))"
pub "$CLOSED" "$T/bad/bridge-read.py"
{ [ "$(rc)" = 3 ] && grep -q "does not compile" "$T/out" && grep -q "SyntaxError" "$T/out"; } \
  && ok "Python with a syntax error is refused, and the error is shown" || no "Python with a syntax error is refused (rc $(rc))"
pub "$CLOSED" "$T/bad/bridge-wait.sh"
[ "$(rc)" = 3 ] && ok "bash with a syntax error is refused" || no "bash with a syntax error is refused (rc $(rc))"
pub "$CLOSED" "$T/bad/owner_ssh_authorized_keys"
{ [ "$(rc)" = 3 ] && grep -q "lock the owner out" "$T/out"; } && ok "an owner key file with no keys is refused" || no "an owner key file with no keys is refused (rc $(rc))"
pub "$CLOSED" "$T/bad/timer.conf" --name dropin.bridge-feeder-net
[ "$(rc)" = 3 ] && ok "a drop-in with a [Timer] section is refused" || no "a drop-in with a [Timer] section is refused (rc $(rc))"
pub "$CLOSED" "$T/bad/not-in-catalog.sh"
{ [ "$(rc)" = 1 ] && grep -q "not an updatable file" "$T/out"; } && ok "a file outside the catalog is refused" || no "a file outside the catalog is refused (rc $(rc))"
pub "$CLOSED" "$T/src/bridge-status.sh" --name
{ [ "$(rc)" = 64 ] && grep -q "usage" "$T/out"; } && ok "--name with nothing after it is refused, not an endless loop" \
  || no "--name with nothing after it is refused, not an endless loop (rc $(rc))"

echo "== part 2: publishing to the real fleet server =="
PY=""
for c in "${FLEET_TEST_PY:-}" "$HOME/netbridge/fleet-test-venv/bin/python" python3; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && "$c" -c 'import fastapi, uvicorn, sqlalchemy, multipart' 2>/dev/null && { PY="$c"; break; }
done
if [ -z "$PY" ]; then
  echo "  SKIP  part 2: FastAPI not installed (set FLEET_TEST_PY or make ~/netbridge/fleet-test-venv)"
else
  PORT="$(free_port)"; FLEET="http://127.0.0.1:$PORT"
  # A clean environment: no SMTP, webhook or Tailscale settings from this shell reach the server.
  ( cd "$REPO/control-plane/backend" && exec env -i PATH="$PATH" HOME="$T/home" \
      DATABASE_URL="sqlite:///$T/fleet.db" PAYLOAD_DIR="$T/payloads" ADMIN_API_KEY="test-admin-key" \
      ALERT_EVAL_INTERVAL_S=3600 OFFLINE_AFTER_S=60 \
      "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning ) >"$T/server.log" 2>&1 &
  SRV=$!
  for _ in $(seq 1 150); do curl -fsS -m 1 "$FLEET/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
  curl -fsS -m 2 "$FLEET/healthz" >/dev/null 2>&1 && ok "fleet server started" || { echo "  FAIL  fleet server did not start"; cat "$T/server.log"; exit 1; }

  served_ok(){ # served_ok <name> <source>: original bytes plus signed context; verify identity and signature
    curl -fsS -m 10 -o "$T/got" "$FLEET/payloads/$1" && curl -fsS -m 10 -o "$T/got.sig" "$FLEET/payloads/$1.sig" \
      && python3 -c 'import pathlib,sys; b=pathlib.Path(sys.argv[1]).read_bytes(); original=b"".join(x for x in b.splitlines(keepends=True) if not x.startswith(b"# NetBridge-Update: ")); sys.exit(original!=pathlib.Path(sys.argv[2]).read_bytes())' "$T/got" "$2" \
      && python3 "$REPO/pi/scripts/bridge-verify-update.py" verify "$T/got" "$T/got.sig" "$K/script-pubkey.pem" "$1" "$T/floors" >/dev/null 2>&1; }
  for f in bridge-web.py bridge-agent.py bridge-status.sh owner_ssh_authorized_keys; do
    pub "$FLEET" "$T/src/$f"
    { [ "$(rc)" = 0 ] && served_ok "$f" "$T/src/$f"; } \
      && ok "$f published; the fleet serves it and the signature verifies with the bridge's key" \
      || no "$f published; the fleet serves it and the signature verifies with the bridge's key (rc $(rc))"
  done
  pub "$FLEET" "$T/src/video.conf" --name dropin.bridge-feeder-net
  { [ "$(rc)" = 0 ] && served_ok dropin.bridge-feeder-net "$T/src/video.conf"; } \
    && ok "a drop-in published under its catalog name" || no "a drop-in published under its catalog name (rc $(rc))"
  pub "$FLEET" "$T/bad/bridge-read.py"
  { [ "$(rc)" = 3 ] && [ ! -e "$T/payloads/bridge-read.py" ]; } && ok "a Python file that does not compile never reaches the fleet" \
    || no "a Python file that does not compile never reaches the fleet (rc $(rc))"
  curl -fsS -m 10 -H "Authorization: Bearer test-admin-key" "$FLEET/admin/payloads" > "$T/list.json" 2>/dev/null
  python3 - "$T/list.json" <<'PY' && ok "the fleet lists all five, each signed" || no "the fleet lists all five, each signed"
import json, sys
got = {x["name"]: x["signed"] for x in json.load(open(sys.argv[1]))}
want = {"bridge-web.py", "bridge-agent.py", "bridge-status.sh", "owner_ssh_authorized_keys", "dropin.bridge-feeder-net"}
sys.exit(0 if set(got) == want and all(got.values()) else 1)
PY
fi

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
