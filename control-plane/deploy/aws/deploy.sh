#!/bin/bash
# Ship the control plane from this Mac to the fleet host. No registry needed: the image is
# built locally, streamed over ssh, and loaded on the far side.
#
#   ./deploy.sh <host>          e.g. ./deploy.sh fleet.yourdomain.com
set -euo pipefail
HOST="${1:?usage: deploy.sh <host-or-ip> [ssh-key]}"
KEY="${2:-$HOME/.ssh/netbridge-fleet.pem}"
CP="$(cd "$(dirname "$0")/../.." && pwd)"          # control-plane/
S="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$HOST"

echo "[deploy] building image from $CP"
docker build -t netbridge-fleet:latest "$CP"

echo "[deploy] streaming image to $HOST (this is the slow part, ~1-2 min)"
docker save netbridge-fleet:latest | gzip | $S 'gunzip | docker load'

echo "[deploy] syncing compose files"
scp -i "$KEY" -o StrictHostKeyChecking=accept-new \
    "$(dirname "$0")/docker-compose.yml" "$(dirname "$0")/Caddyfile" \
    "ubuntu@$HOST:/opt/netbridge/"

echo "[deploy] restarting"
$S 'cd /opt/netbridge && docker compose up -d && sleep 4 && docker compose ps'
echo "[deploy] done — https://$HOST"
