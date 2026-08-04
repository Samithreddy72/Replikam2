#!/bin/bash
# Flip the fleet host from plain HTTP on an IP to real HTTPS on a hostname.
#
# Kept separate from deploy.sh on purpose: bringing the stack up and putting a certificate on
# it are different risks. The stack is verified over HTTP first, then this runs once DNS has
# actually propagated — because Let's Encrypt validates by resolving the name, and a failed
# validation counts against a rate limit that is measured in hours.
set -euo pipefail
DOMAIN="${1:?usage: go-live-domain.sh <fqdn> <host-ip>}"
IP="${2:?need the host ip}"
KEY="${3:-$HOME/.ssh/netbridge-fleet.pem}"
S="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$IP"

echo "[1/4] confirming $DOMAIN resolves to $IP"
GOT="$(dig +short "$DOMAIN" A | tail -1)"
[ "$GOT" = "$IP" ] || { echo "  ✗ $DOMAIN -> '${GOT:-nothing}' (want $IP). DNS has not propagated; not touching Caddy."; exit 1; }
echo "  ✓ resolves correctly"

echo "[2/4] pointing the app and Caddy at the hostname"
$S "sudo sed -i 's|^FLEET_DOMAIN=.*|FLEET_DOMAIN=$DOMAIN|' /opt/netbridge/.env
    grep -q '^PUBLIC_BASE_URL=' /opt/netbridge/.env \
      && sudo sed -i 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$DOMAIN|' /opt/netbridge/.env \
      || echo 'PUBLIC_BASE_URL=https://$DOMAIN' | sudo tee -a /opt/netbridge/.env >/dev/null"

echo "[3/4] restarting (Caddy now requests the certificate)"
$S 'cd /opt/netbridge && sudo docker compose up -d'
sleep 20

echo "[4/4] verifying real HTTPS"
code=$(curl -s -m20 -o /dev/null -w '%{http_code}' "https://$DOMAIN/" || true)
if [ "$code" = "200" ]; then
  echo "  ✓ https://$DOMAIN -> HTTP 200, certificate valid"
  curl -sI -m10 "https://$DOMAIN/" | grep -iE '^HTTP|strict-transport' | sed 's/^/    /'
else
  echo "  ✗ HTTPS not answering yet (got '${code:-none}') — certificate issuance can take a minute"
  $S 'sudo docker logs netbridge-caddy-1 2>&1 | tail -12' | sed 's/^/    /'
fi
