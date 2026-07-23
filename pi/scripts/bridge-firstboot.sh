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
echo "${BRIDGE_VERSION:-dev}" >/etc/bridge/version

# 2b. per-device setup-AP passphrase. Generated HERE (not baked into the image)
# so every unit gets a different key — a shared or SSID-derived key would let
# anyone in radio range join the setup AP and watch the venue's WiFi password
# being typed. `bridge setup-pass` prints it for the label.
if [ ! -s /etc/bridge/setup-wifi-pass ]; then
  LC_ALL=C tr -dc 'ABCDEFGHJKMNPQRSTUVWXYZ23456789' </dev/urandom | head -c 12 \
    >/etc/bridge/setup-wifi-pass
  chmod 600 /etc/bridge/setup-wifi-pass
  log "generated per-device setup-AP passphrase"
fi

# 3. start the agent (enrolls on first tick, then heartbeats)
systemctl enable --now bridge-agent.timer || log "WARN: could not enable bridge-agent.timer"

# 4. one-shot cleanup: destroy the secret conf and never run again
shred -u "$CONF" 2>/dev/null || rm -f "$CONF"
systemctl disable bridge-firstboot.service || true
log "provisioning complete"
