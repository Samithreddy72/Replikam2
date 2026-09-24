#!/bin/bash
# RepliKam adaptive jitter sentry — measures the media path every 20s and
# auto-switches jitter profiles (lan 100/120ms <-> wan 200/200ms) with hysteresis.
# bad sample = loss>2% OR jitter(mdev)>25ms OR rtt>80ms
# 3 consecutive bad -> wan (rough network: bigger buffers beat brief gap)
# It NEVER switches back to lan (owner's standing rule, 2026-09-22: "we never switch pi back to
# the LAN"). Latency is cut elsewhere - the video feeder caps its own buffer at 100 ms whatever
# the profile says. Every switch also restarts the whole media stack, and on an under-powered
# bridge that restart was followed by a reboot 4 times out of 4 (2026-09-22).
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

bad=0; good=0; rtt_floor=""
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
    # RTT threshold, relative to the link's own baseline rather than a fixed 80ms.
    #
    # 80ms was written for a same-city link. India<->US is ~200-250ms on a PERFECT direct
    # path, so a fixed 80 marks a healthy intercontinental link "degraded" on every single
    # sample: `bad` never resets, the sentry pins itself to the WAN profile and stops
    # adapting entirely. WAN is the safe default, so nothing breaks — the sentry just
    # silently becomes a no-op, which is worse than being absent because you would trust it.
    #
    # Learn the floor instead: the best RTT seen this run is the physics of the link, and
    # what matters is DEVIATION from it. Jitter (mdev) and loss stay absolute — those are
    # bad at any distance.
    if [ -n "${avg:-}" ] && { [ -z "${rtt_floor:-}" ] || [ "$avg" -lt "$rtt_floor" ]; }; then
      rtt_floor="$avg"
      LOG "link RTT floor now ${rtt_floor}ms (thresholds follow it)"
    fi
    rtt_bad=$(( ${rtt_floor:-40} * 2 + 40 ))     # e.g. 20ms floor -> 80 (unchanged locally)
    if [ "${loss:-0}" -gt 2 ] || [ "${mdev:-0}" -gt 25 ] || [ "${avg:-0}" -gt "$rtt_bad" ] || [ "${mx:-0}" -gt $((rtt_bad*2)) ]; then bad=$((bad+1)); good=0
    else good=$((good+1)); bad=0; fi
  fi
  # ------------------------------------------------------------------ live-safe response
  #
  # THE GAP THIS CLOSES. Every profile switch below is DEFERRED while media is flowing, and
  # correctly so — a switch restarts the feeders and freezes video. But it also means that
  # during the exact meeting where the network goes bad, this sentry does nothing at all.
  # Observed in the field on 2026-08-12, twice in one session:
  #
  #   DEFERRED profile lan (network pristine for 10min) — a session is live
  #
  # There IS something safe to do while live. The audio the presenter hears is decoded on
  # THEIR laptop, through the app's own buffer, so raising that buffer costs about a second
  # of room audio and touches neither video, nor the USB gadget, nor any service here. The
  # bridge publishes the request and the app adopts it on its next poll (bridge-jitter.py).
  #
  # Marked --auto so it can never overwrite a rung an operator chose by hand, and so only its
  # OWN changes are withdrawn later. A ping looking healthy is not evidence that a human was
  # wrong about what they could hear.
  # DEFAULT OFF as of 2026-08-13, and the reason matters more than the switch.
  #
  # This was written believing the audible fault was LATE audio, which a deeper buffer
  # absorbs. Measurement then showed the opposite: the bridge is losing ~0.7ms of audio per
  # second of stream at the CAPTURE, before anything is sent (0.710 ms/s, 24 of 67 windows
  # short, mean centred at -224ppm so it is episodic loss and not drift). Buffer depth was
  # moved 250 -> 400 -> 600ms by hand and changed nothing audible, which is exactly what you
  # would expect: a jitter buffer repairs audio that arrived late and cannot repair audio
  # that was never recorded.
  #
  # So acting automatically here would add up to 350ms of delay to what the presenter hears,
  # every time the path looks rough, in exchange for nothing. An automatic action that cannot
  # help is worse than none: it moves a number, looks like a fix, and hides the real fault.
  #
  # Turn it on with JITTER_SENTRY_LIVE_RUNG=1 if a link is ever genuinely losing packets in
  # transit — that IS the case buffers are for.
  if [ "${JITTER_SENTRY_LIVE_RUNG:-0}" = "1" ] && [ $bad -ge 3 ] && media_live; then
    # Escalate only after trouble persists: 3 bad samples is ~1 minute, 9 is ~3. A deeper
    # buffer costs real delay in hearing the room, so this must not race upward.
    want=1; [ $bad -ge 9 ] && want=2
    out=$(/usr/local/bin/bridge-jitter.py fix --rung "$want" --auto \
            --why "loss=${loss:-?}% jitter=${mdev:-?}ms rtt=${avg:-?}ms" --json 2>&1)
    case "$out" in
      *'"skipped"'*) : ;;     # already there, or an operator owns it. Do not say so every 20s.
      *) LOG "live: raised presenter buffer to rung $want (loss=${loss:-?}% mdev=${mdev:-?}ms)" ;;
    esac
  fi
  if [ "${JITTER_SENTRY_LIVE_RUNG:-0}" = "1" ] && [ $good -ge 30 ] && ! media_live; then
    # Withdraw only BETWEEN sessions. Changing the buffer mid-call to make things better
    # still costs a gap in the room audio, and nobody thanks a machine for that.
    out=$(/usr/local/bin/bridge-jitter.py reset --auto --json 2>&1)
    case "$out" in *'"skipped"'*) : ;; *) LOG "idle: handed the presenter buffer back" ;; esac
  fi

  if [ $bad -ge 3 ] && [ "${cur:-200}" -lt 300 ]; then
    switch_profile wan "network degraded (loss=$loss% jitter=${mdev}ms rtt=${avg}ms)" && bad=0
  fi
  # (No automatic switch back to lan - see the header. A pristine network is left as it is.)
  sleep 20
done
