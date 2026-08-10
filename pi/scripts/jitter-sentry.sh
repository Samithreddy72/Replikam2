#!/bin/bash
# RepliKam adaptive jitter sentry — measures the media path every 20s and
# auto-switches jitter profiles (lan 100/120ms <-> wan 200/200ms) with hysteresis.
# bad sample = loss>2% OR jitter(mdev)>25ms OR rtt>80ms
# 3 consecutive bad -> wan (rough network: bigger buffers beat brief gap)
# 30 consecutive good on wan -> back to lan (low latency)
LOG(){ logger -t jitter-sentry "$*"; }
PEER_FILE=/etc/default/bridge-return-audio

# NEVER switch profiles while media is flowing.
#
# `bridge profile <x>` rewrites /etc/default/bridge-net and RESTARTS the pipeline —
# uvcd, feeder-net and feeder-audio all stop and come back. Mid-meeting that is a visible
# video freeze and an audible audio gap. This sentry had no liveness check at all, so a
# LIVE session was torn down at 17:30:13 for the most perverse possible reason: the network
# had been PRISTINE for ten minutes, so it "upgraded" to the LAN profile. The reward for a
# stable connection was a dropped stream, and to a presenter it looks like a random fault.
#
# A profile switch is an OPTIMISATION. Nothing breaks by applying it thirty seconds later
# when nobody is on air, and the hysteresis counters are preserved so the decision is not
# lost — only deferred.
#
# "Live" = the video feeder is burning CPU, i.e. RTP is actually arriving. Same signal
# bridge-web's own health check uses, sampled over 1s so it costs almost nothing.
media_live(){
  local pid t0 t1
  pid=$(pgrep -f 'udpsrc port=5000' | head -1)
  [ -n "$pid" ] || return 1
  t0=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null) || return 1
  sleep 1
  t1=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null) || return 1
  [ "${t1:-0}" -gt "${t0:-0}" ]
}

switch_profile(){   # switch_profile <lan|wan> <reason>
  if media_live; then
    LOG "DEFERRED profile $1 ($2) — a session is live; will apply when the stream is idle"
    return 1        # counters kept: we re-evaluate next loop and switch once idle
  fi
  LOG "$2 -> profile $1"
  /usr/local/bin/bridge profile "$1" >/dev/null 2>&1
  return 0
}

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
    switch_profile wan "network degraded (loss=$loss% jitter=${mdev}ms rtt=${avg}ms)" && bad=0
  elif [ $good -ge 30 ] && [ "${cur:-200}" -ge 300 ]; then
    switch_profile lan "network pristine for 10min" && good=0
  fi
  sleep 20
done
