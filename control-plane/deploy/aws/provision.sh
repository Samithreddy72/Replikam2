#!/bin/bash
# First-boot setup for the fleet host. Idempotent — safe to re-run.
#
# Installs Docker, nothing else. The control plane and Caddy both run as containers, so the
# host stays boring: no Python, no nginx, no certbot, nothing to drift.
set -euo pipefail
log(){ echo "[provision] $*"; }

# A FRESH Ubuntu instance runs cloud-init and unattended-upgrades on first boot, and both
# hold the apt lock. Racing them is why the first run of this script died with
# "Could not get lock /var/lib/apt/lists/lock ... held by process (apt-get)". Wait our turn
# instead of failing: every new instance hits this, so it belongs in the script, not in the
# operator's head.
wait_for_apt() {
  command -v cloud-init >/dev/null 2>&1 && cloud-init status --wait >/dev/null 2>&1 || true
  local i
  for i in $(seq 1 60); do          # up to 5 minutes
    if ! fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock \
                /var/lib/dpkg/lock >/dev/null 2>&1; then
      return 0
    fi
    [ $((i % 6)) -eq 1 ] && log "apt is busy (first-boot updates) - waiting..."
    sleep 5
  done
  log "apt still locked after 5 min; continuing anyway"
}

if ! command -v docker >/dev/null 2>&1; then
  wait_for_apt
  log "installing docker"
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker "${SUDO_USER:-ubuntu}" || true
fi
systemctl enable --now docker

mkdir -p /opt/netbridge
log "docker $(docker --version | awk '{print $3}' | tr -d ,) ready"

# Unattended security updates: this box is public and will outlive our attention.
if command -v apt-get >/dev/null 2>&1; then
  wait_for_apt
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
