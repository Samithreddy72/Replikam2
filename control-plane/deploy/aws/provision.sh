#!/bin/bash
# First-boot setup for the fleet host. Idempotent — safe to re-run.
#
# Installs Docker, nothing else. The control plane and Caddy both run as containers, so the
# host stays boring: no Python, no nginx, no certbot, nothing to drift.
set -euo pipefail
log(){ echo "[provision] $*"; }

if ! command -v docker >/dev/null 2>&1; then
  log "installing docker"
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker "${SUDO_USER:-ubuntu}" || true
fi
systemctl enable --now docker

mkdir -p /opt/netbridge
log "docker $(docker --version | awk '{print $3}' | tr -d ,) ready"

# Unattended security updates: this box is public and will outlive our attention.
if command -v apt-get >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades >/dev/null
  systemctl enable --now unattended-upgrades || true
  log "unattended security upgrades enabled"
fi

# A weekly copy of the database next to it. Not a backup strategy on its own — the instance
# snapshot is — but it makes "restore last week's fleet DB" a file copy instead of a restore.
cat > /etc/cron.weekly/netbridge-dbcopy <<'CRON'
#!/bin/sh
d=$(docker volume inspect aws_fleetdata -f '{{.Mountpoint}}' 2>/dev/null) || exit 0
[ -f "$d/bridge.db" ] || exit 0
cp "$d/bridge.db" "$d/bridge.db.weekly"
CRON
chmod +x /etc/cron.weekly/netbridge-dbcopy
log "done — now: cd /opt/netbridge && docker compose up -d"
