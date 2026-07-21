#!/bin/bash
# NetBridge crackle sentry (M4 — walkthrough J4 "Crackle signature detected 4 min
# ago. No one is in a session on this bridge.").
#
# Every CYCLE seconds it forms a verdict on the return-audio path and edge-triggers
# an alert that rides telemetry up to the fleet (bridge-web.py:clock_suspect reads
# the state file this writes). Two signals feed the verdict, in priority order:
#
#   1. xruns  — ALSA under/overruns on the gadget capture stream over the window.
#               A dropped/inserted frame IS the crackle, so this is a hard signal
#               and is always available, session or not (cheap journal + pcm state).
#   2. FFT    — a real spectral+click analysis of a short capture (bridge-clock-fft.py).
#               Only taken when the bridge is IDLE (return-audio not holding the
#               capture device) — which is exactly the walkthrough scenario. When a
#               session is live the device is busy, so we fall back to xruns-only.
#
# Edge-triggered like the control-plane alerting: fire once on clean->bad, resolve
# once on bad->clean, so a persistent crackle doesn't spam. The fix is unchanged:
# `bridge reset-clock`.
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/bin
CYCLE="${CRACKLE_CYCLE:-60}"
CAP_SECS="${CRACKLE_CAPTURE_SECS:-2}"
DEV="hw:UAC2Gadget"
PCM_STATUS=/proc/asound/UAC2Gadget/pcm0c/sub0/status
RUN=/run/bridge; STATE=$RUN/crackle.state; OUT=$RUN/crackle.json
ANALYZER=/usr/local/bin/bridge-clock-fft.py
LOG(){ logger -t crackle-sentry "$*"; }
mkdir -p $RUN
[ -f "$STATE" ] || echo clean > "$STATE"

# xruns over the window: pcm reporting state==XRUN + return-audio I/O errors in the
# journal (both mean the same dropped-frame event, deduped by capping at a sane max).
count_xruns(){
  local x=0 st j
  st=$(grep -c 'state: XRUN' "$PCM_STATUS" 2>/dev/null || echo 0)
  j=$(journalctl -u bridge-return-audio --since "-${CYCLE}s" --no-pager 2>/dev/null \
        | grep -ciE 'overrun|underrun|input/output error|xrun')
  x=$((st + j)); [ "$x" -gt 20 ] && x=20; echo "$x"
}

# idle == return-audio not actively holding pcm0c (so we can open it for a capture)
is_idle(){
  [ "$(systemctl is-active bridge-return-audio 2>/dev/null)" != "active" ] && return 0
  # active service but stream closed (no client attached) still frees the hw device
  grep -q 'state: RUNNING' "$PCM_STATUS" 2>/dev/null && return 1 || return 0
}

verdict_now(){
  local xr wav j
  xr=$(count_xruns)
  if is_idle; then
    wav=$(mktemp /tmp/crackle-XXXX.wav)
    if arecord -D "$DEV" -d "$CAP_SECS" -f S16_LE -r 48000 -c 2 -q "$wav" 2>/dev/null \
         && [ -s "$wav" ]; then
      j=$("$ANALYZER" --xruns "$xr" "$wav" 2>/dev/null)
      rm -f "$wav"; echo "$j"; return
    fi
    rm -f "$wav"
  fi
  # no capture available (session live or capture failed) -> verdict from xruns alone,
  # by handing the analyzer a silent buffer so its scoring path is the single source
  # of truth for thresholds.
  head -c 8000 /dev/zero | "$ANALYZER" --raw --rate 48000 --channels 1 --xruns "$xr" 2>/dev/null
}

sleep 15   # let audio services settle after boot
while true; do
  J=$(verdict_now)
  V=$(echo "$J" | sed -n 's/.*"verdict": *"\([a-z]*\)".*/\1/p')
  [ -z "$V" ] && V=clean
  PREV=$(cat "$STATE" 2>/dev/null || echo clean)
  # collapse degrading+crackle into one "bad" edge; clean is the other side
  cur=bad; [ "$V" = clean ] && cur=good
  prevside=bad; [ "$PREV" = clean ] && prevside=good
  if [ "$cur" = bad ]; then
    # Rewrite every bad cycle (not just the edge) so the file's mtime stays fresh:
    # bridge-web.py:clock_verdict() only trusts it within 5 min, precisely so a dead
    # sentry falls back to the journal heuristic. A long click-only crackle emits no
    # journal I/O errors, so if we only wrote on the edge it would look "resolved"
    # after 5 min while still crackling. LOG only on the edge, though — no spam.
    printf '%s\n' "$J" > "$OUT"
    [ "$prevside" = good ] && LOG "CRACKLE $(echo "$J" | sed -n 's/.*\("score"[^}]*\)/\1/p')"
  elif [ "$prevside" = bad ]; then
    rm -f "$OUT"; LOG "crackle cleared"
  fi
  echo "$V" > "$STATE"
  sleep "$CYCLE"
done
