#!/bin/bash
# Verifying loader: run a signed override from /data if there is one, else the baked-in copy.
#
# WHY THIS EXISTS
# Iterating on one bridge script used to cost a card surgery each time (power down, pull the
# card, mount it on another Pi, edit, swap back, boot — ~10 minutes of a human's hands, and
# seven of them in a single evening during phase 6). The root filesystem is read-only with an
# overlay, so anything written to / is lost at reboot; /data is the only durable writable
# store. This loader makes /data the deployment target for SCRIPTS, the same way A/B
# partitions are the target for IMAGES.
#
# TRUST MODEL
# An override is executed ONLY if openssl verifies its detached signature against
# /data/config/script-pubkey.pem (EC/SHA256 — the same primitive bridge-update.sh uses for
# images). No signature, wrong signature, or missing pubkey => the baked-in script runs.
# Failure is always toward the version that shipped on the card, never toward unverified code.
#
# AUTO-ROLLBACK (the thing that makes remote deploy safe)
# A remote script deploy can brick a bridge in a way a card swap cannot: push a script that
# crashes, and the service crash-loops with nobody on site. So every start under an override
# is stamped, and if a service starts $TRIP_N times within $TRIP_WINDOW_S the override is
# QUARANTINED (moved aside, not deleted) and the baked-in script takes over. The bridge heals
# itself and the evidence is preserved for diagnosis.
set -uo pipefail

NAME="${1:?usage: bridge-run.sh <script-name> [args...]}"; shift || true
BAKED="/usr/local/bin/$NAME"
DIR="/data/overrides"
PUBKEY="/data/config/script-pubkey.pem"
STATE="/data/overrides/.state"
TRIP_N="${BRIDGE_RUN_TRIP_N:-3}"
TRIP_WINDOW_S="${BRIDGE_RUN_TRIP_WINDOW_S:-120}"

log() { echo "bridge-run[$NAME]: $*" >&2; }

# Reject anything that is not a bare filename: this argument reaches a path, and the caller
# is a systemd unit today but could be something less careful tomorrow.
case "$NAME" in
  */*|.*|"") log "refusing suspicious script name"; exit 64 ;;
esac

exec_baked() { exec "$BAKED" "$@"; }
[ -x "$BAKED" ] || { log "baked-in script missing: $BAKED"; exit 127; }

OVR="$DIR/$NAME"
SIG="$DIR/$NAME.sig"
[ -f "$OVR" ] || exec_baked "$@"

if [ ! -f "$PUBKEY" ]; then
  log "override present but no pubkey at $PUBKEY — running baked-in"
  exec_baked "$@"
fi
if [ ! -f "$SIG" ]; then
  log "override present but UNSIGNED — running baked-in (refusing unverified code)"
  exec_baked "$@"
fi
if ! openssl dgst -sha256 -verify "$PUBKEY" -signature "$SIG" "$OVR" >/dev/null 2>&1; then
  log "override SIGNATURE VERIFY FAILED — running baked-in"
  exec_baked "$@"
fi

# ---- signature is good; now the crash-loop trip ----
mkdir -p "$STATE" 2>/dev/null
STAMP="$STATE/$NAME.starts"
now=$(date +%s)
# Keep only starts inside the window, then add this one. A healthy service starts once and
# its stamp ages out; a crash-looping one accumulates.
recent=""
if [ -f "$STAMP" ]; then
  while read -r t; do
    [ -n "$t" ] || continue
    [ $((now - t)) -le "$TRIP_WINDOW_S" ] && recent="$recent$t
"
  done < "$STAMP"
fi
recent="$recent$now
"
printf '%s' "$recent" > "$STAMP" 2>/dev/null
n=$(printf '%s' "$recent" | grep -c . )

if [ "$n" -ge "$TRIP_N" ]; then
  q="$DIR/quarantine"
  mkdir -p "$q" 2>/dev/null
  mv -f "$OVR" "$q/$NAME.$now" 2>/dev/null
  mv -f "$SIG" "$q/$NAME.sig.$now" 2>/dev/null
  rm -f "$STAMP" 2>/dev/null
  # Machine-readable so bridge-web can surface it in telemetry and the fleet can alert.
  printf '{"script":"%s","starts":%s,"window_s":%s,"ts":%s}\n' \
    "$NAME" "$n" "$TRIP_WINDOW_S" "$now" > "$DIR/.quarantined.json" 2>/dev/null
  log "OVERRIDE QUARANTINED — $n starts in ${TRIP_WINDOW_S}s; reverting to baked-in"
  exec_baked "$@"
fi

log "running signed override (start $n/${TRIP_N} in window)"
exec "$OVR" "$@"
