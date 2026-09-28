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
# TRUST ANCHOR on the READ-ONLY root, not on the writable data partition. Putting it in
# /data was a security hole: overrides live in /data too, so anyone able to write there could
# replace the key and then sign their own code. Root is mounted ro, so the anchor is immutable
# for exactly the party this mechanism defends against. /data is kept only as a fallback for
# cards imaged before this change, and is checked SECOND so it can never override the anchor.
PUBKEY="/etc/netbridge/script-pubkey.pem"
[ -f "$PUBKEY" ] || PUBKEY="/data/config/script-pubkey.pem"
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

# Safe mode (bridge-overrides.sh): three boots in a row never became healthy, so this boot runs
# every built-in script and no override at all — whatever was installed last cannot keep the
# bridge down. A healthy boot clears it.
SAFE_FLAG="${BRIDGE_RUN_SAFE_FLAG:-/run/bridge-overrides/safe-mode}"
if [ -e "$SAFE_FLAG" ]; then
  log "safe mode — running the built-in script"
  exec_baked "$@"
fi

OVR="$DIR/$NAME"
SIG="$DIR/$NAME.sig"
[ -f "$OVR" ] || exec_baked "$@"

# Installed on another OS (2026-09-28). /data survives an OS update, and a script written for the
# old OS must not run on top of the new one: the new OS's built-in runs. Back on the old OS (a
# rollback) it runs again; deploying it again adopts it. bridge-overrides.sh (read-only root,
# like this loader) decides, so the loader and the boot-time binds always agree - asked here at
# every start rather than read from a list its boot run writes, which a failed boot run would
# leave empty. The voice and return-audio scripts carry across as they always have.
OVERRIDES="${BRIDGE_RUN_OVERRIDES:-/usr/local/bin/bridge-overrides.sh}"
if "$OVERRIDES" superseded "$NAME" 2>/dev/null; then
  log "override was installed on another OS version — running the built-in script"
  exec_baked "$@"
fi

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

# Time this against the BOOT, not the wall clock.
#
# This used to stamp with `date +%s`. Services start long before NTP syncs, so every boot
# stamped very nearly the SAME wall-clock value (fake-hwclock restores one time and the
# journal shows them all as "Jun 18 01:27:1x"). The stamps live on /data and survive
# reboots — so after three POWER CYCLES the counter saw "3 starts in 120s", declared a
# crash loop, and quarantined a perfectly healthy override. It happened: a routine power
# cycle silently reverted a field bridge to its baked-in script, with one log line as the
# only evidence.
#
# A crash loop is by definition repeated starts WITHIN one boot — systemd restarts a
# failing unit immediately, so per-boot counting catches it exactly as well. /proc/uptime
# is monotonic and resets when the machine does, which is precisely the semantics wanted.
# The boot id is recorded too so stamps from a previous boot are discarded rather than
# aged out, making the reset explicit instead of a side effect of arithmetic.
# Both sources are overridable so the reboot case is testable — without that, the very bug
# this fixes is the one the suite cannot reach.
UPTIME_SRC="${BRIDGE_RUN_UPTIME_SRC:-/proc/uptime}"
BOOTID_SRC="${BRIDGE_RUN_BOOTID_SRC:-/proc/sys/kernel/random/boot_id}"
now=$(cut -d' ' -f1 "$UPTIME_SRC" 2>/dev/null | cut -d. -f1)
now="${now:-0}"
BOOT_ID="$(cat "$BOOTID_SRC" 2>/dev/null)"
BOOT_ID="${BOOT_ID:-unknown}"

recent=""
if [ -f "$STAMP" ]; then
  stamped_boot="$(head -n1 "$STAMP" 2>/dev/null)"
  if [ "$stamped_boot" = "$BOOT_ID" ]; then
    # same boot: keep only starts still inside the window. Fed by here-doc, NOT a pipe —
    # a pipe would run this loop in a subshell and `recent` would come back empty.
    while read -r t; do
      [ -n "$t" ] || continue
      case "$t" in *[!0-9]*) continue ;; esac
      [ $((now - t)) -le "$TRIP_WINDOW_S" ] && recent="$recent$t
"
    done <<EOF
$(tail -n +2 "$STAMP" 2>/dev/null)
EOF
  fi
  # different boot id -> `recent` stays empty: a reboot is NOT a crash loop
fi
recent="$recent$now
"
# Line 1 is the boot id; the rest are boot-relative start times. Writing the id every time
# means the NEXT boot reads a mismatch and starts counting from zero.
printf '%s\n%s' "$BOOT_ID" "$recent" > "$STAMP" 2>/dev/null
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
