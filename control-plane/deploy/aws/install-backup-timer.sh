#!/usr/bin/env bash
# Run on the Fleet host after deploying an image containing app.backup.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo on the Fleet host' >&2; exit 1; }
cd /opt/netbridge
docker compose exec -T fleet python -m app.backup
install -d /etc/systemd/system
cat > /etc/systemd/system/netbridge-backup.service <<'EOF'
[Unit]
Description=Consistent local NetBridge Fleet backup
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
WorkingDirectory=/opt/netbridge
ExecStart=/usr/bin/docker compose exec -T fleet python -m app.backup
TimeoutStartSec=10min
EOF
cat > /etc/systemd/system/netbridge-backup.timer <<'EOF'
[Unit]
Description=Daily NetBridge Fleet database backup
[Timer]
OnCalendar=daily
RandomizedDelaySec=30min
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now netbridge-backup.timer
