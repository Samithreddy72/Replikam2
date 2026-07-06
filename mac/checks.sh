#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  RepliKam presenter checks — the four green checks from the product vision.
#  Every check is a REAL measurement made on the bridge, not a guess.
#
#  Usage:  bash checks.sh [bridge-address]     (default: PI env or developer.conf)
#          bash checks.sh --watch              refresh every 5 s
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
[ -f "$D/developer.conf" ] && . "$D/developer.conf" 2>/dev/null || true
PI="${1:-${PI:-${BRIDGE:-100.91.108.50}}}"
[ "$PI" = "--watch" ] && PI="${PI_DEFAULT:-${BRIDGE:-100.91.108.50}}"
WATCH=0; [[ "${1:-}" == "--watch" || "${2:-}" == "--watch" ]] && WATCH=1

render() {
  S=$(curl -s -m 6 "http://$PI:8080/api/status" 2>/dev/null)
  echo ""
  echo "  RepliKam presenter checks · bridge $PI"
  echo "  ─────────────────────────────────────────────────────"
  if [ -z "$S" ]; then
    echo "  ✗ 1. Bridge online          — UNREACHABLE"
    echo "       fix: is the bridge powered? are you on the same Tailscale network?"
    echo ""
    return
  fi
  echo "$S" | python3 -c "
import json, sys
d = json.load(sys.stdin)
flow = d.get(\"flow\") or {}
udc = d.get(\"udc\") or \"?\"
ret_fps = flow.get(\"return_from_client_fps\", 0)

def line(ok, n, text, fix):
    mark = chr(10003) if ok else chr(10007)
    print(\"  %s %d. %s\" % (mark, n, text))
    if not ok and fix:
        print(\"       fix: %s\" % fix)

line(True, 1, \"Bridge online                    (%s - %s)\" % (d.get(\"host\",\"?\"), d.get(\"pairing_code\",\"\")), \"\")
line(flow.get(\"video_in\", False), 2,
     \"Your video arriving at bridge\",
     \"start GO-LIVE on this machine; if running, check this machines Wi-Fi\")
line(udc == \"configured\", 3,
     \"Meeting laptop sees the camera   (USB %s)\" % udc,
     \"plug the USB cable from the bridge into the meeting laptop\")
line(ret_fps > 40000, 4,
     \"Meeting audio flowing back       (%d frames/s)\" % ret_fps,
     \"on the meeting laptop set Speakers to Source/Sink and play something\")
voice = flow.get(\"voice_in\", False)
mic = flow.get(\"mic_to_client_fps\", 0)
print(\"       (voice: %s, mic feed %d frames/s)\" % (\"arriving\" if voice else \"not arriving\", mic))
"
  echo ""
}

if [ "$WATCH" = 1 ]; then
  while true; do clear; render; sleep 5; done
else
  render
fi
