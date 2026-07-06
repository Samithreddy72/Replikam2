#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  RepliKam presenter checks — the four green checks from the product vision.
#  Every check is a REAL measurement made on the bridge, not a guess:
#  GET /api/checks samples feeder CPU ticks + gadget hw_ptr deltas for ~2s
#  on the bridge itself (lifetime averages like `ps pcpu` lie about "now").
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
  # /api/status is instant (labels); /api/checks blocks ~2s while the bridge samples.
  S=$(curl -s -m 6 "http://$PI:8080/api/status" 2>/dev/null)
  C=$(curl -s -m 10 "http://$PI:8080/api/checks" 2>/dev/null)
  echo ""
  echo "  RepliKam presenter checks · bridge $PI"
  echo "  ─────────────────────────────────────────────────────"
  if [ -z "$S" ] || [ -z "$C" ]; then
    echo "  ✗ 1. Bridge online          — UNREACHABLE"
    echo "       fix: is the bridge powered? are you on the same Tailscale network?"
    echo ""
    return
  fi
  python3 -c "
import json, sys
d = json.loads(sys.argv[1])
c = json.loads(sys.argv[2])

def get(key):
    v = c.get(key) or {}
    return bool(v.get(\"ok\")), v.get(\"detail\", \"\")

def line(ok, n, text, fix):
    mark = chr(10003) if ok else chr(10007)
    print(\"  %s %d. %s\" % (mark, n, text))
    if not ok and fix:
        print(\"       fix: %s\" % fix)

line(True, 1, \"Bridge online                    (%s - %s)\" % (d.get(\"host\",\"?\"), d.get(\"pairing_code\",\"\")), \"\")
v_ok, v_det = get(\"video_arriving\")
line(v_ok, 2,
     \"Your video arriving at bridge    (%s)\" % v_det,
     \"start GO-LIVE on this machine; if running, check this machines Wi-Fi\")
u_ok, u_det = get(\"client_sees_camera\")
line(u_ok, 3,
     \"Meeting laptop sees the camera   (%s)\" % u_det,
     \"plug the USB cable from the bridge into the meeting laptop\")
a_ok, a_det = get(\"return_audio\")
line(a_ok, 4,
     \"Meeting audio flowing back       (%s)\" % a_det,
     \"on the meeting laptop set Speakers to Source/Sink and play something\")
print(\"\")
print(\"  %s\" % (\"ALL GREEN — you are live.\" if v_ok and u_ok and a_ok else \"not live yet — fix the ✗ items above.\"))
" "$S" "$C"
  echo ""
}

if [ "$WATCH" = 1 ]; then
  while true; do clear; render; sleep 5; done
else
  render
fi
