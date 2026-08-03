#!/bin/bash
# /usr/sbin is REQUIRED: alsactl lives there on Debian/RPi OS, and without it the
# rate follower dies silently at startup (monitor pipe closes, loop never runs).
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
[ -f /etc/default/bridge-return-audio ] && . /etc/default/bridge-return-audio
# Tuning knobs live in their OWN file: `bridge set-peer` rewrites bridge-return-audio
# wholesale on every go-live, so anything stored there would be silently wiped. Written by
# `bridge return-tune` (bridge-web /api/return-tune) — remote pipeline experiments without
# card surgery.
[ -f /etc/default/bridge-return-tune ] && . /etc/default/bridge-return-tune
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

CARD="${RETURN_CARD:-UAC2Gadget}"
FIXED_RATE="${RETURN_FIXED_RATE:-48000}"
# PATH is pinned above for systemd determinism, so the external tools are named through
# variables rather than found on a caller-supplied PATH. Production behaviour is unchanged;
# the off-hardware test substitutes stubs.
GST="${RETURN_GST:-gst-launch-1.0}"
AMIXER="${RETURN_AMIXER:-amixer}"
ALSACTL="${RETURN_ALSACTL:-alsactl}"
# Only follow rates the gadget actually advertises. Opening hw: at a bogus rate takes the
# return audio down completely, so an implausible reading is ignored, never acted on.
ALLOWED_RATES="${RETURN_ALLOWED_RATES:-32000 44100 48000}"
DEBOUNCE_S="${RETURN_DEBOUNCE_S:-1}"
# THE safety net. A previous attempt polled /proc and cycled the pipeline whenever it
# thought the rate moved; re-opening a live ALSA capture over and over destroyed the audio
# (measured 21.6 dropouts/sec, vs 0.2 with no follower at all). Even if every other guard
# here fails, this caps re-opens to one per interval.
# 8s, down from 20: the gap exists to stop restart CASCADES, and since blocked changes
# are now DEFERRED (not dropped), a long window only delays legitimate switches - at 20s
# a user hopping rates waited up to 20s for audio to land. 8s still caps any cascade at
# ~7 restarts/min worst case while feeling immediate to a human.
MIN_RESTART_GAP_S="${RETURN_MIN_RESTART_GAP_S:-8}"
RUNDIR="${RETURN_RUNDIR:-/run/bridge-return-audio}"
mkdir -p "$RUNDIR" 2>/dev/null || RUNDIR=/tmp

# ------------------------------------------------------------------ rate detection
# Walkthrough J3 step 6: "at whatever rate the meeting laptop happens to play".
#
# Read the kernel's OWN control rather than inferring. drivers/usb/gadget/function/
# u_audio.c exposes a per-direction PCM control "<Playback|Capture> Rate" holding the
# host's active sample rate (0 when the host is not streaming or the cable is out), and
# calls snd_ctl_notify() on every active<->inactive transition - so it is authoritative
# AND event-driven.
#
# The failed attempt parsed /proc/asound/.../hw_params. That reflects the rate OUR OWN
# capture opened the device at - self-referential, silent about the host, and "closed"
# mid-restart. Polling it produced phantom changes and a restart cascade. Do not go back.
# The kernel registers the rate controls on iface=PCM (u_audio.c:
# SNDRV_CTL_ELEM_IFACE_PCM), while `amixer cget name=...` searches iface=MIXER by
# default — so the plain-name lookup reports "no such control" for a control that exists.
# That is exactly what happened on the first hardware boot: the journal said
# "no 'Capture Rate' control" on a 6.12 kernel that certainly ships it, and the script
# fail-safed. Query iface=PCM first; keep the plain form as a fallback for any older
# kernel that registered it differently.
_ctl_get() {
  "$AMIXER" -c "$CARD" cget "iface=PCM,name='Capture Rate'" 2>/dev/null \
    || "$AMIXER" -c "$CARD" cget name="Capture Rate" 2>/dev/null
}
have_ctl() {
  if _ctl_get >/dev/null 2>&1; then return 0; fi
  # Make the NEXT diagnostics bundle decisive instead of another guess: list what rate
  # controls this card actually has, right in the journal.
  echo "bridge-return-audio: rate controls visible on $CARD:" >&2
  "$AMIXER" -c "$CARD" controls 2>/dev/null | grep -i rate >&2 || echo "  (none)" >&2
  return 1
}
host_rate() {
  # POSIX basic-regex only: \+ is a GNU extension and silently matches nothing under BSD
  # sed, which made host_rate() return empty and the follower a no-op on the test machine.
  # ONLY the value line ('  : values=44100'). amixer cget also prints a type line
  # ('; type=INTEGER,...,values=1,...') where values=1 is the COUNT of values — the old
  # pattern matched that line first and returned '1', which failed validation and silently
  # fell back to 48000 while the host streamed 44.1k: every start died not-negotiated.
  # The test stub only printed the value line, which is why 14/14 passed against a parser
  # that fails on the real device.
  _ctl_get | sed -n 's/^ *: *values=\([0-9][0-9]*\).*/\1/p' | head -1
}
rate_ok() { case " $ALLOWED_RATES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# ------------------------------------------------------------------ pipeline
# hw: (never plughw). The plug layer's resampler is what made the return audio noisy; any
# conversion belongs in GStreamer's audioresample quality=10, a real polyphase SRC. At 48k
# it is a pass-through and costs nothing.
start_pipeline() {
  local rate="$1"
  echo "bridge-return-audio: capture @ ${rate} Hz -> ${DEST_IP}:${DEST_PORT}" >&2
  # RETURN_SRC_PROPS: extra alsasrc properties (e.g. "slave-method=none provide-clock=false").
  # RETURN_PRE_RESAMPLE: elements spliced in before audioresample (e.g. "audiorate").
  # Both exist to chase the resampler clicks heard at followed rates (44.1/32k) — the
  # clicks appear exactly when audioresample is active and never at 48k pass-through, and
  # the candidate fixes are all src/timestamp properties. Runtime-tunable so each candidate
  # is one API call + service restart, not one card surgery. UNQUOTED on purpose:
  # word-splitting is the mechanism; bridge-web validates the charset.
  local pre=""
  [ -n "${RETURN_PRE_RESAMPLE:-}" ] && pre="! $RETURN_PRE_RESAMPLE "
  [ -n "${RETURN_SRC_PROPS:-}" ] && echo "bridge-return-audio: src props: $RETURN_SRC_PROPS" >&2
  [ -n "$pre" ] && echo "bridge-return-audio: pre-resample: $RETURN_PRE_RESAMPLE" >&2
  "$GST" alsasrc device="hw:$CARD" buffer-time=200000 latency-time=20000 ${RETURN_SRC_PROPS:-} \
    ! "audio/x-raw,rate=$rate" \
    ! queue max-size-time=300000000 leaky=downstream \
    ! audioconvert $pre! audioresample quality=10 \
    ! audio/x-raw,rate=48000,channels=2,format=S16LE \
    ! opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20 \
    ! rtpopuspay pt=97 \
    ! udpsink host="$DEST_IP" port="$DEST_PORT" sync=false &
  GST_PID=$!
  echo "$GST_PID" > "$RUNDIR/gst.pid"
}

cleanup() {
  [ -n "${WD_PID:-}" ] && kill "$WD_PID" 2>/dev/null
  [ -n "${MON_PID:-}" ] && kill "$MON_PID" 2>/dev/null
  [ -n "${GST_PID:-}" ] && kill "$GST_PID" 2>/dev/null
  exit 0
}
trap cleanup TERM INT

# ------------------------------------------------------------------ fail safe
# Without the kernel control we cannot know the host's rate, so run exactly the proven
# fixed-rate pipeline and exec so systemd owns the process directly. Never guess - guessing
# is what broke the audio last time.
if ! have_ctl; then
  echo "bridge-return-audio: no 'Capture Rate' control on card $CARD - fixed ${FIXED_RATE} Hz" >&2
  exec "$GST" alsasrc device="hw:$CARD" buffer-time=200000 latency-time=20000 \
    ! queue max-size-time=300000000 leaky=downstream \
    ! audioconvert ! audioresample quality=10 \
    ! audio/x-raw,rate=48000,channels=2,format=S16LE \
    ! opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20 \
    ! rtpopuspay pt=97 ! udpsink host="$DEST_IP" port="$DEST_PORT" sync=false
fi

R="$(host_rate)"; rate_ok "${R:-0}" || R="$FIXED_RATE"   # 0 = host idle right now
echo "$R" > "$RUNDIR/rate"

# ------------------------------------------------------------------ follower
# Runs in the BACKGROUND and does exactly two things: publish the wanted rate, and ask the
# current pipeline to stop. It deliberately does NOT own the pipeline - a `cmd | while`
# loop runs in a subshell, so any PID or state it kept would be invisible to the main
# shell. That subshell trap is what made the CI secret-sweep a silent no-op; the same
# mistake here would leave an orphan gst holding hw: while the parent starts a second one.
# The last-re-open timestamp lives in a FILE, not a shell variable. `cmd | while` puts the
# loop in a subshell, and reasoning about which assignments survive that boundary is exactly
# the class of bug that made the CI secret-sweep a silent no-op and cost hours tonight.
# A file is unambiguous.
echo 0 > "$RUNDIR/last_restart"
(
  "$ALSACTL" monitor "$CARD" 2>/dev/null | while read -r _l; do
    case "$_l" in *Rate*) ;; *) continue ;; esac
    sleep "$DEBOUNCE_S"                          # let USB enumeration settle
    new="$(host_rate)"
    [ -n "$new" ] && [ "$new" != "0" ] || continue    # host stopped: keep streaming
    cur="$(cat "$RUNDIR/rate" 2>/dev/null)"
    [ "$new" != "$cur" ] || continue
    rate_ok "$new" || { echo "bridge-return-audio: ignoring implausible rate $new" >&2; continue; }
    now=$(date +%s)
    last="$(cat "$RUNDIR/last_restart" 2>/dev/null)"; last="${last:-0}"
    if [ $((now - last)) -lt "$MIN_RESTART_GAP_S" ]; then
      # DEFER, never drop. This limiter once DISCARDED a 48->32 switch that arrived inside
      # the window; the control then sat steady at 32000 so no further event ever came, and
      # the pipeline ran 44.1k-caps against a 32k device indefinitely - alive, errorless,
      # and audibly ROBOTIC at 72% speed (36 RTP pkts/s instead of 50). Sleep out the
      # window, re-read the control, and apply if the change is still real.
      wait_s=$((MIN_RESTART_GAP_S - (now - last)))
      echo "bridge-return-audio: ${cur}->${new} deferred ${wait_s}s (re-opened recently)" >&2
      sleep "$wait_s"
      new="$(host_rate)"
      cur="$(cat "$RUNDIR/rate" 2>/dev/null)"
      [ -n "$new" ] && [ "$new" != "0" ] && [ "$new" != "$cur" ] && rate_ok "$new" || continue
      now=$(date +%s)
    fi
    echo "$now" > "$RUNDIR/last_restart"
    echo "bridge-return-audio: host switched ${cur} -> ${new} Hz; following" >&2
    echo "$new" > "$RUNDIR/rate"
    kill "$(cat "$RUNDIR/gst.pid" 2>/dev/null)" 2>/dev/null   # main loop restarts it
  done
) &
MON_PID=$!

# ------------------------------------------------------------------ supervisor
# The MAIN shell owns the pipeline: start it, wait for it to exit, start it again at
# whatever rate is currently published. This also covers a plain gst crash, which the old
# fixed-rate script got for free from systemd's Restart=.
fails=0
while true; do
  # THE LIVE KERNEL CONTROL IS AUTHORITATIVE ON EVERY START — the published file is only a
  # fallback for when the host is idle (control reads 0), and FIXED_RATE the last resort.
  #
  # Learned the hard way (2026-07-31, on hardware): a race during a Windows rate switch
  # left the file saying 48000 while the host held the stream open at 44100. Every restart
  # then failed alsasrc not-negotiated, and because the follower only reacts to CHANGE
  # events — and the control sat steady at 44100 — no event ever arrived to correct the
  # file. Crash-loop, audio down, until a human flipped Windows back to 48000. Reading the
  # control here makes every restart self-correcting: the event stream is for promptness,
  # never for correctness.
  R="$(host_rate)"
  if ! rate_ok "${R:-0}"; then
    R="$(cat "$RUNDIR/rate" 2>/dev/null)"; rate_ok "${R:-0}" || R="$FIXED_RATE"
  fi
  echo "$R" > "$RUNDIR/rate"
  started=$(date +%s)
  start_pipeline "$R"
  # Event-loss-proof reconcile: while the pipeline runs, periodically compare the LIVE
  # control against the rate it was opened at. A mismatched-but-alive pipeline throws no
  # error - u_audio keeps the old-session stream running when the host changes rate - so
  # neither the crash path nor the event path can catch it. This watchdog can.
  (
    while kill -0 "$GST_PID" 2>/dev/null; do
      # 10s, down from 30: measured on hardware, single rate switches sometimes settle via
      # THIS watchdog rather than the event path (the user waited 15-20s). The check is one
      # amixer read - cheap enough to run 6x/min - and it bounds worst-case adaptation at
      # ~10-12s instead of ~30.
      sleep 10
      live="$(host_rate)"
      [ -n "$live" ] && [ "$live" != "0" ] || continue
      rate_ok "$live" || continue
      [ "$live" = "$(cat "$RUNDIR/rate" 2>/dev/null)" ] && continue
      echo "bridge-return-audio: mismatch watchdog - device at ${live}, pipeline at $(cat "$RUNDIR/rate" 2>/dev/null); re-opening" >&2
      echo "$live" > "$RUNDIR/rate"
      kill "$GST_PID" 2>/dev/null
      break
    done
  ) &
  WD_PID=$!
  wait "$GST_PID"
  kill "$WD_PID" 2>/dev/null; wait "$WD_PID" 2>/dev/null
  ran=$(( $(date +%s) - started ))
  # Crash-looping (not a rate change) - hand back to systemd rather than spin here.
  if [ "$ran" -lt 5 ]; then
    fails=$((fails+1))
    [ "$fails" -ge 5 ] && { echo "bridge-return-audio: pipeline failing immediately - exiting for systemd" >&2; cleanup; }
    sleep 2
  else
    fails=0
  fi
done
