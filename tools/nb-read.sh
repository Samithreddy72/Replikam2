#!/bin/bash
# Read a file on the bridge from here, in one command.
#
#     bash tools/nb-read.sh /proc/asound/UAC2Gadget/pcm0c/sub0/status
#     bash tools/nb-read.sh --list /data
#
# Read-only. The DEVICE enforces which paths are readable, refuses credential files, and
# redacts anything token-shaped — this script only queues the request and prints the answer.
set -uo pipefail
DEV="${NB_DEVICE:-100000005d5ade42}"
API="${NB_FLEET:-https://fleet.scine.online}"
TOK=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.netbridge-source/state.json')))['token'])" 2>/dev/null)
[ -n "$TOK" ] || { echo "no fleet token — sign in from the app first" >&2; exit 2; }

ARGS='{}'
if [ "${1:-}" = "--list" ]; then
  ARGS=$(python3 -c "import json,sys;print(json.dumps({'path':sys.argv[1],'list':True}))" "${2:?usage: --list <dir>}")
else
  ARGS=$(python3 -c "import json,sys;print(json.dumps({'path':sys.argv[1]}))" "${1:?usage: nb-read.sh <path>}")
fi

ID=$(curl -s -m 25 -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d "{\"type\":\"read-file\",\"args\":$ARGS}" "$API/admin/devices/$DEV/commands" \
     | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))")
[ -n "$ID" ] || { echo "could not queue the read" >&2; exit 3; }

for _ in $(seq 1 20); do
  sleep 6
  OUT=$(curl -s -m 25 -H "Authorization: Bearer $TOK" "$API/admin/devices/$DEV/commands?limit=5" \
        | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    if c.get('id')==$ID and c.get('status') in ('done','error','rejected'):
        print(c.get('output') or '(no output)'); break")
  [ -n "$OUT" ] && { echo "$OUT"; exit 0; }
done
echo "timed out waiting for the bridge" >&2; exit 4
