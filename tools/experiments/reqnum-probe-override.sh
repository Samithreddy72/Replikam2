#!/bin/bash
# TEMPORARY EXPERIMENT — raise the UAC2 gadget's req_number, then hand off unchanged.
#
# WHAT THIS IS FOR
# ----------------
# req_number is how many USB requests the audio gadget keeps queued. At a 1ms service
# interval it is, in effect, how many milliseconds the gadget can absorb the driver being
# late before an isochronous slot is missed — and a missed slot is audio that is GONE, not
# delayed. The card is running 8. Measured on this hardware while streaming, with no camera
# in use at any point:
#
#     0.710 ms/s of audio never captured, 24 of 67 windows short, worst window ~5ms
#
# Gaps of 2-5ms against 8ms of runway is what running out of queued requests looks like.
# This raises it to 32 so the hypothesis can be tested TODAY, without waiting for an image.
#
# WHY IT IS SHAPED LIKE THIS
# --------------------------
# It does NOT contain a copy of the media pipeline. It changes one configfs value and then
# execs the baked-in script. Duplicating a pipeline into an override is exactly how this
# project produced its last two audio outages (an S16LE reference branch that refused to
# link, and an empty variable that left "! !" in the middle of a pipeline), and a copy here
# would silently go stale the moment the real script changes.
#
# COST WHEN IT RUNS: changing req_number requires the gadget to be unbound and rebound, so
# the meeting laptop's camera, microphone and speakers disappear and must be re-selected —
# the same cost as `reset-clock`. It is a one-shot: on every later start the value already
# matches and this falls straight through to the baked-in script.
#
# TO REMOVE IT: Actions -> "Undo a bad script -> revert to factory" (bridge-return-audio.sh).
# The card then returns to req_number=8 at its next gadget rebuild or reboot.
set -uo pipefail
BAKED=/usr/local/bin/bridge-return-audio.sh
G=/sys/kernel/config/usb_gadget/g1
F="$G/functions/uac2.usb0/req_number"
WANT="${UAC2_REQ_NUMBER:-32}"
log(){ echo "reqnum-probe: $*" >&2; }

# ACT AT MOST ONCE. The first version of this script gated on "is the value different yet",
# which looked reasonable and was the bug: configfs REFUSES this write while the function is
# linked into a configuration, so the value never changed, the condition stayed true, and
# every restart unbound and rebound the gadget again. The meeting laptop lost its devices
# repeatedly and the bridge fell off the network. Nothing crashed, so auto-rollback never
# fired either — it watches for a script that dies, and this one exited cleanly while doing
# damage.
#
# The marker is written BEFORE the risky part, on purpose. If this wedges the bridge and it
# gets power-cycled, the marker is already on disk and the next boot does not repeat it.
MARK=/data/.experiment-reqnum.done
if [ -f "$MARK" ]; then
  log "already attempted once (marker present) — running audio unchanged"
elif [ -w "$F" ] && [ -w "$G/UDC" ]; then
  : > "$MARK" 2>/dev/null; sync 2>/dev/null
  CUR="$(cat "$F" 2>/dev/null || echo '')"
  if [ -n "$CUR" ] && [ "$CUR" != "$WANT" ]; then
    UDC="$(cat "$G/UDC" 2>/dev/null || echo '')"
    if [ -n "$UDC" ]; then
      log "raising req_number ${CUR} -> ${WANT} (gadget rebinds; client re-selects devices)"
      echo "" > "$G/UDC" 2>/dev/null || log "unbind failed"
      sleep 1
      if echo "$WANT" > "$F" 2>/dev/null; then log "req_number now $(cat "$F" 2>/dev/null)"
      else log "write refused — leaving as ${CUR}"; fi
      # ALWAYS rebind, whatever happened above. A gadget left unbound is a bridge that has
      # vanished from the meeting laptop entirely, which is far worse than the fault we are
      # investigating.
      echo "$UDC" > "$G/UDC" 2>/dev/null || log "REBIND FAILED — run reset-clock"
      sleep 2
    else
      log "no UDC bound yet; leaving req_number alone"
    fi
  else
    log "req_number already ${CUR:-unknown}; nothing to do"
  fi
else
  log "configfs not writable here; running audio unchanged"
fi
# NOTE: this experiment is superseded. req_number is set by uvc-raw-setup.sh at boot, before
# the function is linked, which is the only point it can actually be changed. It ships in the
# image. This file is kept as the worked example behind tools/experiments/README.md.

exec "$BAKED" "$@"
