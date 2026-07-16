#!/bin/bash
# RepliKam adaptive jitter sentry — measures the media path every 20s and
# auto-switches jitter profiles (lan 100/120ms <-> wan 200/200ms) with hysteresis.
# bad sample = loss>2% OR jitter(mdev)>25ms OR rtt>80ms
# 3 consecutive bad -> wan (rough network: bigger buffers beat brief gap)
# 30 consecutive good on wan -> back to lan (low latency)
LOG(){ logger -t jitter-sentry "$*"; }
PEER_FILE=/etc/default/bridge-return-audio
bad=0; good=0
sleep 30
while true; do
  PEER=$(grep -oE "RETURN_DEST_IP=[0-9.]+" $PEER_FILE 2>/dev/null | cut -d= -f2)
  [ -z "$PEER" ] && PEER=$(ip route | awk "/default/{print \$3; exit}")
  R=$(ping -c 8 -i 0.25 -W2 "$PEER" 2>/dev/null | tail -2)
  loss=$(echo "$R" | grep -oE "[0-9]+(\.[0-9]+)?% packet loss" | grep -oE "^[0-9]+" | head -1)
  mx=$(echo "$R" | grep -oE "mdev = [0-9.]+/[0-9.]+/[0-9.]+/[0-9.]+" | cut -d/ -f3 | cut -d. -f1)
  mdev=$(echo "$R" | grep -oE "mdev = [0-9.]+/[0-9.]+/[0-9.]+/[0-9.]+" | cut -d/ -f4 | cut -d. -f1)
  avg=$(echo "$R" | grep -oE "= [0-9.]+/[0-9.]+" | cut -d/ -f2 | cut -d. -f1)
  cur=$(grep -o "NET_AUDIO_LATENCY=[0-9]*" /etc/default/bridge-net 2>/dev/null | cut -d= -f2)
  if [ -z "$loss" ]; then bad=$((bad+1)); else
    if [ "${loss:-0}" -gt 2 ] || [ "${mdev:-0}" -gt 25 ] || [ "${avg:-0}" -gt 80 ] || [ "${mx:-0}" -gt 150 ]; then bad=$((bad+1)); good=0
    else good=$((good+1)); bad=0; fi
  fi
  if [ $bad -ge 3 ] && [ "${cur:-200}" -lt 300 ]; then
    LOG "network degraded (loss=$loss% jitter=${mdev}ms rtt=${avg}ms) -> profile WAN"
    /usr/local/bin/bridge profile wan >/dev/null 2>&1; bad=0
  elif [ $good -ge 30 ] && [ "${cur:-200}" -ge 300 ]; then
    LOG "network pristine for 10min -> profile LAN"
    /usr/local/bin/bridge profile lan >/dev/null 2>&1; good=0
  fi
  sleep 20
done
