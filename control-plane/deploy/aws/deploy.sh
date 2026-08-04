#!/bin/bash
# Ship the control plane to the fleet host.
#
# The image is built ON THE HOST, not here. Building locally would need Docker on the Mac
# (it is not installed) and would then stream a ~200 MB image over ssh on every deploy. The
# source is a few hundred KB, and the host already has Docker because provision.sh put it
# there — so we send source and build where it runs.
set -euo pipefail
HOST="${1:?usage: deploy.sh <host-or-ip> [ssh-key]}"
KEY="${2:-$HOME/.ssh/netbridge-fleet.pem}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CP="$(cd "$HERE/../.." && pwd)"                       # control-plane/
S="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$HOST"

echo "[deploy] sending source (backend + built panel + Dockerfile)"
tar -C "$CP" -czf - Dockerfile backend/requirements.txt backend/app panel-dist \
  | $S 'sudo mkdir -p /opt/netbridge/src && sudo chown -R ubuntu:ubuntu /opt/netbridge && tar -C /opt/netbridge/src -xzf -'

echo "[deploy] sending compose files"
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new \
    "$HERE/docker-compose.yml" "$HERE/Caddyfile" "ubuntu@$HOST:/opt/netbridge/"

echo "[deploy] building on the host"
$S 'cd /opt/netbridge/src && sudo docker build -q -t netbridge-fleet:latest . | tail -1'

echo "[deploy] starting"
$S 'cd /opt/netbridge && sudo docker compose up -d && sleep 5 && sudo docker compose ps'
echo "[deploy] done"
