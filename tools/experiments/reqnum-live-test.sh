#!/bin/bash
# LIVE TEST: raise the UAC2 gadget's req_number to 32, once, then hand off unchanged.
#
# Deployed as a signed override named bridge-return-audio.sh. It changes one gadget value
# and then execs the baked-in script — it contains no copy of the media pipeline, because
# duplicated pipelines are how this project produced two audio outages.
#
# WHY req_number
# --------------
# At a 1ms service interval it is, in effect, how many milliseconds the gadget can absorb the
# driver being late before an isochronous slot is missed — and a missed slot is audio that is
# GONE, not delayed. The card runs 8. Measured while streaming, no camera in use:
#
#     0.710 ms/s never captured, 24 of 67 windows short, worst window ~5ms
#
# 2-5ms gaps against 8ms of runway is what running out of queued requests looks like.
#
# WHY THE EARLIER ATTEMPT FAILED (2026-08-13)
# -------------------------------------------
# It unbound the UDC and wrote req_number. configfs REFUSED: the attribute is read-only while
# the function is LINKED INTO A CONFIGURATION, and unbinding the UDC does not unlink it. The
# value never changed, the script gated on "is it different yet", so every service start
# unbound and rebound the gadget — the meeting laptop lost its devices repeatedly and the
# bridge fell off the network. Nothing crashed, so auto-rollback never fired.
#
# This version fixes both halves:
#   * it does the FULL sequence — unbind, unlink from the config, write, relink, rebind
#   * it is SELF-LIMITING on a marker written BEFORE the risky part, so a power-cycle mid-way
#     does not repeat it. It runs at most once, ever, whatever the outcome.
set -uo pipefail
BAKED=/usr/local/bin/bridge-return-audio.sh
G=/sys/kernel/config/usb_gadget/g1
FN=uac2.usb0
CFG="$G/configs/c.1"
REQ="$G/functions/$FN/req_number"
WANT="${UAC2_REQ_NUMBER:-32}"
MARK=/data/.experiment-reqnum32.done
log(){ echo "reqnum-live: $*" >&2; }

hand_off(){ exec "$BAKED" "$@"; }

[ -f "$MARK" ] && { log "already attempted (marker present) — unchanged"; hand_off "$@"; }
[ -e "$REQ" ] && [ -d "$CFG" ] || { log "gadget layout not as expected — unchanged"; hand_off "$@"; }

CUR="$(cat "$REQ" 2>/dev/null || echo '')"
[ "$CUR" = "$WANT" ] && { log "req_number already $WANT — unchanged"; hand_off "$@"; }

UDC="$(cat "$G/UDC" 2>/dev/null || echo '')"
[ -n "$UDC" ] || { log "no UDC bound — not touching the gadget"; hand_off "$@"; }

# Past this point the gadget WILL be torn down, so claim the attempt before taking any risk.
: > "$MARK" 2>/dev/null; sync 2>/dev/null
log "attempting ${CUR} -> ${WANT}; the client will re-enumerate its devices once"

# Always put the gadget back, whatever happens in between. A bridge left unbound has
# vanished from the meeting laptop entirely — far worse than the fault being investigated.
restore(){
  [ -e "$CFG/$FN" ] || ln -s "../../functions/$FN" "$CFG/$FN" 2>/dev/null || log "RELINK FAILED"
  echo "$UDC" > "$G/UDC" 2>/dev/null || log "REBIND FAILED — run reset-clock from the fleet"
  sleep 2
  log "req_number now $(cat "$REQ" 2>/dev/null || echo '?'), UDC=$(cat "$G/UDC" 2>/dev/null)"
}
trap restore EXIT

echo "" > "$G/UDC" 2>/dev/null || log "unbind failed"
sleep 1
rm -f "$CFG/$FN" 2>/dev/null || log "unlink failed"
sleep 1
if echo "$WANT" > "$REQ" 2>/dev/null; then log "write accepted"; else log "write REFUSED even when unlinked"; fi

trap - EXIT
restore
hand_off "$@"
