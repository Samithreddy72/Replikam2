#!/bin/bash
# NetBridge first-boot provisioning. Runs ONCE on a freshly flashed Pi:
#   1. makes tailscaled ready (running, but NOT joined — no key on the card),
#   2. writes the fleet-agent config (CONTROL_URL + one-time bootstrap token),
#   3. enables the agent timer (which enrolls over HTTPS + starts heartbeating),
#   4. shreds the secret-bearing conf and disables itself.
# KEY-AT-CLAIM (docs/PROVISIONING-V2.md): the tailnet key is NOT on the SD card.
# The agent enrolls over the public control-plane HTTPS URL with only a bootstrap
# token, the admin claims the device, and the tailnet key is delivered ONCE via
# GET /v1/provision, which bridge-agent's apply_provision() runs as `tailscale up`.
# So a lost card is no longer a live tailnet key.
set -euo pipefail
log(){ logger -t bridge-firstboot "$*"; echo "bridge-firstboot: $*"; }

# 0. grow /data (p4, the LAST partition) to fill the card. The factory disk image
# (factory/build-disk-image.sh) ships a minimal p4 so the .img stays small; expand it
# once so the DB/logs/tailscale state get the whole card. Idempotent + only ever touches
# partition 4 (never rootA/rootB). Marker lives on /data so a reflash re-expands but an
# OTA (which never rewrites /data) does not. Runs before the provision-conf early-exit.
DISK=/dev/mmcblk0
if mountpoint -q /data && [ -b "${DISK}p4" ] && [ ! -f /data/.expanded ]; then
  if command -v growpart >/dev/null 2>&1; then
    growpart "$DISK" 4 && log "grew ${DISK}p4" || log "growpart: no change (already full?)"
  elif command -v parted >/dev/null 2>&1; then
    parted -s "$DISK" resizepart 4 100% && log "resized ${DISK}p4 (parted)" || log "parted resizepart: no change"
  else
    # Don't fail silently: without a partition tool /data stays at its factory size
    # and the card's remaining space is simply unusable.
    log "ERROR: neither growpart nor parted present — /data CANNOT expand; image is missing cloud-guest-utils/parted"
  fi
  partprobe "$DISK" 2>/dev/null || true
  resize2fs "${DISK}p4" && log "resize2fs ${DISK}p4 done" || log "resize2fs: no change"
  touch /data/.expanded 2>/dev/null || true
fi

# ---------------------------------------------------------------------------------------
# Everything from here to the CONF check must NOT depend on a provisioning file, because on
# these images there ISN'T one. The fleet config is seeded onto /data at build time instead,
# so `exit 0` below fires on every single card - and it used to sit ABOVE this work, which
# meant the hostname rename and the version stamp never ran anywhere. Every bridge stayed
# "raspberrypi" and every bridge reported version "dev". Enrolment still worked, which is
# precisely why nobody noticed for weeks.
# ---------------------------------------------------------------------------------------

# Per-device identity (hostname, version stamp) now lives in bridge-identity.sh and runs from
# its own unit on EVERY boot. It used to sit here, which worked only by accident: the
# `systemctl disable` at the end of this script is unreachable while no provisioning file
# exists, so firstboot happens to run every time. The day a provision conf appears, firstboot
# would self-disable and the hostname would silently stop being applied - on a read-only root
# there is no file holding it, so it must be set at each boot by something that always runs.
/usr/local/bin/bridge-identity.sh || log "identity step failed"

if [ -n "${PC:-}" ]; then
  NEWHOST="netbridge-${PC}"
  if [ "$(hostname)" != "$NEWHOST" ]; then
    # THE ROOT IS READ-ONLY ext4 (/dev/mmcblk0p2 / ext4 ro,relatime), not an overlay.
    #
    # So /etc/hostname cannot be written, and `hostnamectl set-hostname` - which tries to
    # write it - appears to SUCCEED anyway: it returned 0 on every card while changing
    # nothing, so the `||` fallback that would have worked was never reached. Every bridge
    # shipped as "raspberrypi", which collides on mDNS the moment two share a venue LAN.
    #
    # Two fixes, and the second is the one that matters:
    #   * set the kernel hostname directly. sethostname(2) touches no filesystem, so it works
    #     on a read-only root, and it is what avahi and mDNS actually answer with.
    #   * VERIFY by reading the hostname back instead of trusting an exit code. That is the
    #     failure this had: a command that reports success and does nothing.
    #
    # Persistence comes from this script running on every boot, not from a file. On a
    # read-only root that is the honest mechanism: nothing to write, nothing to drift.
    hostnamectl set-hostname "$NEWHOST" >/dev/null 2>&1 || true
    [ "$(hostname)" = "$NEWHOST" ] || hostname "$NEWHOST" 2>/dev/null || true
    # /etc/hostname is on the read-only root; this succeeds only if something has made it
    # writable, and is skipped silently otherwise. The kernel name above is what matters.
    [ -w /etc/hostname ] && echo "$NEWHOST" >/etc/hostname 2>/dev/null || true
    [ -w /etc/hosts ] && sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1\\t$NEWHOST/" /etc/hosts 2>/dev/null || true

    if [ "$(hostname)" = "$NEWHOST" ]; then
      log "hostname -> $NEWHOST"
    else
      # Say so loudly rather than leaving a silent no-op to be discovered months later by
      # two bridges answering to the same name.
      log "ERROR: hostname is still $(hostname), wanted $NEWHOST - mDNS will collide if a second bridge joins this LAN"
    fi
  fi
fi


CONF=/boot/firmware/bridge-provision.conf
[ -f "$CONF" ] || CONF=/boot/bridge-provision.conf
[ -f "$CONF" ] || { log "no provision conf found; nothing to do"; exit 0; }
# shellcheck disable=SC1090
. "$CONF"

# 1. tailscaled ready but UNJOINED. The card carries no tailnet key; the join
# happens later, when the admin claims this device and apply_provision runs
# `tailscale up --authkey=<key delivered at claim>`. We only need the daemon up
# and enabled so that call succeeds the moment the key arrives.
if command -v tailscale >/dev/null 2>&1; then
  systemctl enable --now tailscaled || log "WARN: could not start tailscaled"
else
  log "WARN: tailscale not installed"
fi
# A legacy card may still carry TS_AUTHKEY. Ignore it by design — the whole point
# of key-at-claim is that the key is never on the card. Warn so it's noticed.
# NB: must be a full `if`, not `[ -n ... ] && log`, because under `set -e` the
# test failing (the normal keyless case) would abort the whole script.
if [ -n "${TS_AUTHKEY:-}" ]; then
  log "NOTE: TS_AUTHKEY present in conf but IGNORED (key-at-claim); remove it from provisioning"
fi

# 2. fleet-agent config
install -d -m 755 /etc/bridge
umask 077
cat >/etc/default/bridge-agent <<EOF
CONTROL_URL=${CONTROL_URL:-}
BOOTSTRAP_TOKEN=${BOOTSTRAP_TOKEN:-}
EOF
chmod 600 /etc/default/bridge-agent
# Only overwrite the version CI baked in if provisioning actually supplies one. The old
# line wrote "dev" whenever BRIDGE_VERSION was unset - which is every card, since the
# provision conf does not carry it - so the one field that identifies an image destroyed
# itself on first boot. On 2026-08-14 a running bridge had to be identified by fingerprinting
# an unrelated bug in its status output, because /etc/bridge/version said "dev".

# 2b. setup-AP passphrase. NOT written here any more: it is derived per device from the
# CPU serial by bridge-derive-pass, which every consumer calls. Writing a value here was
# how "bridge2626" became a FLEET-WIDE shared passphrase - one leaked label would have
# unlocked the setup AP of every bridge we ship. A stored random value was no better: it
# lives on /data, so a reflash destroyed it and silently invalidated the printed label.

# 3. start the agent (enrolls on first tick, then heartbeats)
systemctl enable --now bridge-agent.timer || log "WARN: could not enable bridge-agent.timer"

# 4. one-shot cleanup: destroy the secret conf and never run again
shred -u "$CONF" 2>/dev/null || rm -f "$CONF"
systemctl disable bridge-firstboot.service || true
log "provisioning complete"
