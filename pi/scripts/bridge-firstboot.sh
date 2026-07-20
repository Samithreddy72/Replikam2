#!/bin/bash
# NetBridge first-boot provisioning. Runs ONCE on a freshly flashed Pi:
#   1. joins the tailnet with a tagged pre-auth key,
#   2. writes the fleet-agent config (CONTROL_URL + one-time bootstrap token),
#   3. enables the agent timer (which enrolls + starts heartbeating),
#   4. shreds the secret-bearing conf and disables itself.
# Secrets arrive via a conf file dropped on the boot partition at flash time
# (see provisioning/inject-secrets.sh). Nothing secret is baked into the repo/image.
set -euo pipefail
log(){ logger -t bridge-firstboot "$*"; echo "bridge-firstboot: $*"; }

CONF=/boot/firmware/bridge-provision.conf
[ -f "$CONF" ] || CONF=/boot/bridge-provision.conf
[ -f "$CONF" ] || { log "no provision conf found; nothing to do"; exit 0; }
# shellcheck disable=SC1090
. "$CONF"

# 1. join the tailnet (tag:bridge identity comes from the auth key's ACL tags)
if command -v tailscale >/dev/null 2>&1 && [ -n "${TS_AUTHKEY:-}" ]; then
  systemctl enable --now tailscaled || true
  tailscale up --authkey="$TS_AUTHKEY" \
    --hostname="${TS_HOSTNAME:-bridge-$(hostname)}" \
    --accept-dns=false \
    || log "WARN: tailscale up failed; will retry on next boot if conf kept"
else
  log "WARN: tailscale not installed or TS_AUTHKEY missing"
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
