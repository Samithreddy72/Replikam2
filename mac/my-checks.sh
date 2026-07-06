#!/usr/bin/env bash
# my-checks.sh — presenter green checks (Journey 3: "am I live?").
# Asks the bridge for four real-activity checks and pretty-prints them.
# The Pi samples actual CPU-tick / hw_ptr deltas for ~2s, so this takes ~3s.
#
# Usage:  ./my-checks.sh              (Pi tailscale IP default)
#         PI=192.168.1.100 ./my-checks.sh
set -euo pipefail

PI="${PI:-100.91.108.50}"
URL="http://${PI}:8080/api/checks"

JSON=$(curl -fsS --max-time 15 "$URL") || {
  echo "✗ bridge unreachable at $URL"
  echo "  (office wifi flaps sometimes — wait ~30s and try again; the Pi self-heals)"
  exit 1
}

python3 - "$JSON" <<'PY'
import json, sys

d = json.loads(sys.argv[1])
LABELS = [
    ("online",             "Bridge online"),
    ("video_arriving",     "Video arriving from your machine"),
    ("client_sees_camera", "Meeting laptop sees the camera"),
    ("return_audio",       "Hearing the room (return audio)"),
]
all_ok = True
for key, label in LABELS:
    c = d.get(key, {})
    ok = bool(c.get("ok"))
    all_ok = all_ok and ok
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(" %s %-34s %s" % (mark, label, c.get("detail", "")))
print()
print("\033[32m ALL GREEN — you are live.\033[0m" if all_ok
      else "\033[31m NOT LIVE YET — fix the ✗ items above.\033[0m")
sys.exit(0 if all_ok else 2)
PY
