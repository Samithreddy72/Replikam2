#!/bin/bash
# NetBridge preflight — run this BEFORE going live.
#
# Answers, in order, the four questions that cost hours on 2026-08-10/11:
#   1. Is the Mac's camera actually delivering frames?      (it opened but sent nothing)
#   2. Is the bridge online and did it just reboot?         (uptime went 3h41m -> 15m)
#   3. How OFTEN is the board browning out?                (0.5% = clean, 2%+ = audible)
#   4. Are all four media gates green at the FAR end?       (not "is a socket bound")
#
# Exit 0 = clear to go live. Exit 1 = something needs attention, and it says what.

APP=http://127.0.0.1:8765
CTRL=http://127.0.0.1:18080
PROBLEMS=0
note(){ printf '  %s\n' "$*"; }
bad(){ printf '  \033[31m✗\033[0m %s\n' "$*"; PROBLEMS=$((PROBLEMS+1)); }
good(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

echo
echo "NetBridge preflight — $(date '+%a %d %b %H:%M')"
echo "========================================"
echo

# ---------------------------------------------------------------- 1. the app
echo "1. Presenter app"
if curl -s -m 4 "$APP/api/state" >/dev/null 2>&1; then
  good "running"
else
  bad "not running — double-click 'Launch NetBridge.command' first, then re-run this"
  echo; exit 1
fi

LIVE=$(curl -s -m 6 "$APP/api/state" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('live'))" 2>/dev/null)
note "live: $LIVE"
echo

# ------------------------------------------------------------- 2. the camera
echo "2. Mac camera"
# Only meaningful once live: the app owns the camera, and probing it from here would
# contend with the encoder. Before go-live, check nothing else has grabbed it.
HOLD=$(lsof 2>/dev/null | grep -iE "AppleH1[0-9]CamIn" | awk '{print $1}' | sort -u | tr '\n' ' ')
if [ -n "$HOLD" ]; then
  warn "these apps hold the camera: $HOLD"
  note "close them, or the encoder will start and send nothing"
else
  good "free — nothing else is holding it"
fi
if [ "$LIVE" = "True" ]; then
  V=$(for p in $(pgrep -f ffmpeg); do ps -o args= -p $p 2>/dev/null | grep -q ':5000' && echo $p; done | head -1)
  if [ -n "$V" ]; then
    A=$(ps -o time= -p "$V" | tr -d ' '); sleep 4; B=$(ps -o time= -p "$V" | tr -d ' ')
    if [ "$A" = "$B" ]; then
      bad "video encoder is STALLED (no CPU in 4s) — camera is not delivering frames"
      note "fix: bash ~/Desktop/fix-camera.sh   (or restart the Mac)"
    else
      good "video encoder is producing ($A -> $B)"
    fi
  fi
fi
echo

# ------------------------------------------------------------- 3. the bridge
echo "3. Bridge"
ST=$(curl -s -m 12 --http1.0 "$CTRL/api/status" 2>/dev/null)
if [ -z "$ST" ]; then
  warn "no mesh route yet — that is normal before go-live (the helper starts with the session)"
  note "re-run this after Go live for the full picture"
else
  python3 - "$ST" <<'PY'
import json,sys
d=json.loads(sys.argv[1]) if sys.argv[1].strip().startswith("{") else {}
up=d.get("uptime","?")
print("  \033[32m✓\033[0m online — uptime %s, %s, wifi %s dBm" % (up, d.get("temp","?"), d.get("wifi","?")))
if "minute" in str(up) and "hour" not in str(up):
    print("  \033[33m!\033[0m uptime is under an hour — it may have rebooted recently")
p=d.get("power") or {}
r=p.get("rate") or {}
if r:
    pct=r.get("pct")
    # Measured on this board: 0.50% was clean by ear, 2.33% was audible and accelerating.
    band = "GOOD - this is your best-state zone" if pct < 1.0 else \
           ("MARGINAL - watch it" if pct < 2.0 else "BAD - stutter expected")
    print("  brownout rate: %.2f%% of the last %d seconds  -> %s" % (pct, r.get("samples",0), band))
if p.get("ok") is False:
    print("  \033[31m✗\033[0m POWER: %s" % p.get("summary"))
    for f in p.get("flags",[]): print("      - %s" % f)
    print("      raise the return buffer to 400ms; expect stutter until this is resolved")
elif p.get("ok") is True:
    print("  \033[32m✓\033[0m power clean since boot (%s)" % p.get("raw"))
else:
    print("  \033[33m!\033[0m power verdict unavailable — bridge predates the sticky-bits fix.")
    print("      Its 'throttled' field reads 0x0 even while browning out. Trust a bundle, not that field.")
PY
fi
echo

# -------------------------------------------------------------- 4. the gates
echo "4. Media gates (measured at the bridge, not inferred here)"
CH=$(curl -s -m 15 --http1.0 "$CTRL/api/checks" 2>/dev/null)
if [ -z "$CH" ]; then
  warn "not reachable — go live first, then re-run"
else
  python3 - "$CH" <<'PY'
import json,sys
try: d=json.loads(sys.argv[1])
except Exception: print("  ! unreadable"); raise SystemExit
bad=0
for k in ("online","video_arriving","voice_arriving","client_sees_camera","return_audio"):
    v=d.get(k)
    if not isinstance(v,dict): continue
    ok=v.get("ok")
    print("  %s %-20s %s" % ("\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m", k, v.get("detail")))
    if not ok: bad+=1
raise SystemExit(1 if bad else 0)
PY
  [ $? -ne 0 ] && PROBLEMS=$((PROBLEMS+1))
fi
echo

# ------------------------------------------------------------ 5. path quality
if [ -n "$ST" ]; then
  echo "5. Mesh path quality (jitter lives here)"
  IP=$(python3 -c "import json,sys;print((json.loads(sys.argv[1]) or {}).get('tailscale_ip',''))" "$ST" 2>/dev/null)
  if [ -n "$IP" ]; then
    R=$(ping -c 20 -i 0.2 "$IP" 2>/dev/null | tail -2)
    echo "$R" | sed 's/^/     /'
    SD=$(echo "$R" | grep -oE "[0-9.]+/[0-9.]+/[0-9.]+/[0-9.]+" | cut -d/ -f4 | cut -d. -f1)
    if [ -n "$SD" ] && [ "$SD" -ge 8 ]; then
      bad "jitter is high (stddev ${SD}ms) — raise the return buffer:"
      note "curl -s -X POST $APP/api/return-tuning -H 'Content-Type: application/json' -d '{\"jitter_ms\":400}'"
    elif [ -n "$SD" ]; then
      good "path is steady (stddev ${SD}ms)"
    fi
  fi
  echo
fi

echo "========================================"
if [ $PROBLEMS -eq 0 ]; then
  printf '  \033[32mCLEAR TO GO LIVE\033[0m\n\n'; exit 0
else
  printf '  \033[31m%d ISSUE(S) — see above\033[0m\n\n' "$PROBLEMS"; exit 1
fi
