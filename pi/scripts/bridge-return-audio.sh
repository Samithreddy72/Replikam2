#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
[ -f /etc/default/bridge-return-audio ] && . /etc/default/bridge-return-audio
# No hardcoded fallback IP. 192.168.29.49 was one developer's laptop on one LAN in one
# month; on every other card it meant the bridge quietly streamed the client's audio to a
# stranger's address on the local network and reported no error. If no peer is set, send
# nowhere and say so - the /api/checks return_audio probe is what surfaces it.
DEST_IP="${RETURN_DEST_IP:-}"
DEST_PORT="${RETURN_DEST_PORT:-5004}"
if [ -z "$DEST_IP" ]; then
  echo "bridge-return-audio: no RETURN_DEST_IP set (run: bridge set-peer <ip>) - not streaming" >&2
  exec sleep infinity
fi

# ------------------------------------------------------- rate following (walkthrough phase 6)
# J3 step 6: "at whatever rate the meeting laptop happens to play - 32, 44.1, or 48 kHz",
# backstage: "switched live, no restarts".
#
# THE TRAP THIS AVOIDS. An earlier build advertised 48000,44100 and sounded noisy, so the
# gadget was pinned to a single 48k rate. The culprit was never "the client chose 44.1" - it
# was resampling THROUGH ALSA's plug layer (plughw), whose SRC is cheap and audible. So we
# still never touch plughw: we open hw: AT THE RATE THE HOST ACTUALLY NEGOTIATED and let
# GStreamer's audioresample (quality=10, a real polyphase SRC) do the single conversion up
# to the 48k Opus works in. That keeps the "no plug layer" property that fixed the noise,
# without forcing every client to be a 48k device.
#
# Opus is internally 48k, so a 48k client stays a pass-through and costs nothing; 44.1/32
# pay one high-quality conversion instead of being refused.
CARD="${RETURN_CARD:-UAC2Gadget}"
FALLBACK_RATE="${RETURN_FALLBACK_RATE:-48000}"
POLL_S="${RETURN_RATE_POLL_S:-2}"
GST_PID=""

# What rate is the host streaming right now? While the client plays, the gadget's CAPTURE
# substream publishes the negotiated rate in hw_params. Nothing playing => "closed" => no
# rate to follow; the caller keeps the current one rather than thrashing.
# Base dir is overridable so the follow-loop can be exercised off-hardware (see
# tests/test-return-rate-follow.sh); on a real bridge it is always /proc/asound.
ASOUND="${RETURN_ASOUND_BASE:-/proc/asound}"
current_rate() {
  local hp r
  for hp in "$ASOUND"/card*/pcm*c/sub0/hw_params; do
    [ -r "$hp" ] || continue
    case "$(cat "$hp" 2>/dev/null)" in *closed*) continue ;; esac
    r=$(sed -n 's/^rate: *\([0-9]\{4,6\}\).*/\1/p' "$hp" 2>/dev/null | head -1)
    [ -n "$r" ] && { echo "$r"; return; }
  done
  echo ""
}

# PATH is pinned above for systemd determinism, so the binary is named through a variable
# rather than found on a caller-supplied PATH — that keeps the production behaviour fixed
# while letting the off-hardware test substitute a stub.
GST="${RETURN_GST:-gst-launch-1.0}"

start_pipeline() {
  local rate="$1"
  # hw: (never plughw) - see above. audioresample quality=10 does rate -> 48k for Opus.
  "$GST" \
    alsasrc device="hw:$CARD" buffer-time=200000 latency-time=20000 \
    ! "audio/x-raw,rate=$rate" \
    ! queue max-size-time=300000000 leaky=downstream \
    ! audioconvert ! audioresample quality=10 \
    ! audio/x-raw,rate=48000,channels=2,format=S16LE \
    ! opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20 \
    ! rtpopuspay pt=97 \
    ! udpsink host="$DEST_IP" port="$DEST_PORT" sync=false &
  GST_PID=$!
}

stop_pipeline() {
  [ -n "$GST_PID" ] || return 0
  kill "$GST_PID" 2>/dev/null
  # Wait for hw: to actually be released. Re-opening while the old handle lingers fails
  # with -EBUSY, which presents as "the return audio just died" with no obvious cause.
  local i=0
  while [ $i -lt 20 ] && kill -0 "$GST_PID" 2>/dev/null; do sleep 0.05; i=$((i+1)); done
  kill -9 "$GST_PID" 2>/dev/null
  wait "$GST_PID" 2>/dev/null
  GST_PID=""
}

trap 'stop_pipeline; exit 0' TERM INT

RATE="$(current_rate)"; RATE="${RATE:-$FALLBACK_RATE}"
echo "bridge-return-audio: starting at ${RATE} Hz -> ${DEST_IP}:${DEST_PORT}" >&2
start_pipeline "$RATE"

# "Switched live, no restarts" is a promise to the PRESENTER: nobody restarts anything, the
# bridge follows the client on its own. A rate change is a hardware renegotiation, so the
# capture element genuinely must be re-opened - we just do it automatically, in well under a
# second, and ONLY when the rate actually changed.
while true; do
  sleep "$POLL_S"
  if ! kill -0 "$GST_PID" 2>/dev/null; then
    echo "bridge-return-audio: pipeline exited - restarting at ${RATE} Hz" >&2
    start_pipeline "$RATE"
    continue
  fi
  NEW="$(current_rate)"
  # Empty = client not playing. Never tear down on that: a pause between songs would
  # otherwise cycle the pipeline forever. Only a real, different rate matters.
  [ -n "$NEW" ] || continue
  [ "$NEW" = "$RATE" ] && continue
  echo "bridge-return-audio: client switched ${RATE} -> ${NEW} Hz; following" >&2
  stop_pipeline
  RATE="$NEW"
  start_pipeline "$RATE"
done
