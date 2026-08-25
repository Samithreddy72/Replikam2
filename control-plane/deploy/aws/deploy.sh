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

# ---------------------------------------------------------------------------------------
# WHAT IS BEING DEPLOYED, AND CAN WE PROVE IT AFTERWARDS?
#
# Until 26 Aug 2026 this script sent a tarball, built it on the host and reported success --
# and nothing anywhere recorded WHICH COMMIT that was. The consequences were not theoretical:
# /docs was closed in source and stayed open in production for days, and the only way anyone
# found out was by probing the live server's behaviour. "Fixed in the repo" and "fixed in
# production" were indistinguishable from outside.
#
# Worse, it would cheerfully deploy a DIRTY working tree, producing a running control plane
# that matched no commit at all.
GIT_SHA="$(git -C "$CP/.." rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git -C "$CP/.." status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILD_ID="${BUILD_ID:-manual-$(date -u +%Y%m%d%H%M%S)}"

if [ "$DIRTY" != "0" ] && [ "${ALLOW_DIRTY:-}" != "1" ]; then
  echo "[deploy] REFUSING: the working tree has $DIRTY uncommitted change(s)." >&2
  echo "         Deploying now would put code in production that matches no commit, and" >&2
  echo "         nothing afterwards could tell you what is running." >&2
  echo "         Commit first, or re-run with ALLOW_DIRTY=1 to deploy deliberately." >&2
  exit 1
fi
[ "$DIRTY" != "0" ] && GIT_SHA="${GIT_SHA}-dirty"

echo "[deploy] commit   $GIT_SHA"
echo "[deploy] build id $BUILD_ID"

echo "[deploy] sending source (backend + built panel + Dockerfile)"
tar -C "$CP" -czf - Dockerfile backend/requirements.txt backend/app panel-dist \
  | $S 'sudo mkdir -p /opt/netbridge/src && sudo chown -R ubuntu:ubuntu /opt/netbridge && tar -C /opt/netbridge/src -xzf -'

echo "[deploy] sending compose files"
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new \
    "$HERE/docker-compose.yml" "$HERE/Caddyfile" "ubuntu@$HOST:/opt/netbridge/"

echo "[deploy] building on the host"
$S "cd /opt/netbridge/src && sudo docker build -q \
      --build-arg CONTROL_PLANE_GIT_SHA='$GIT_SHA' \
      --build-arg CONTROL_PLANE_BUILD_ID='$BUILD_ID' \
      --build-arg CONTROL_PLANE_BUILT_AT='$BUILT_AT' \
      -t netbridge-fleet:latest . | tail -1"

echo "[deploy] starting"
$S 'cd /opt/netbridge && sudo docker compose up -d && sleep 5 && sudo docker compose ps'

# ---------------------------------------------------------------------------------------
# PROVE IT. `docker compose ps` says a container is up, which is the same class of green
# light as `{"ok": true}` -- it does not say the NEW code is serving. Ask the live server
# what it is, and fail if it is not what we just sent.
echo "[deploy] verifying what is actually live"
for i in $(seq 1 20); do
  LIVE="$(curl -fsS -m 10 "https://${DOMAIN:-fleet.scine.online}/healthz" 2>/dev/null || true)"
  echo "$LIVE" | grep -q '"ok"' && break
  sleep 3
done
LIVE_SHA="$(printf '%s' "$LIVE" | sed -n 's/.*"git_sha" *: *"\([^"]*\)".*/\1/p')"
if [ -z "$LIVE_SHA" ]; then
  echo "[deploy] WARNING: the live server did not report a git_sha. Either it is running a" >&2
  echo "         build from before this identity work, or it is not the server we just" >&2
  echo "         deployed to. Do NOT record this deploy as verified." >&2
  exit 1
fi
if [ "$LIVE_SHA" != "$GIT_SHA" ]; then
  echo "[deploy] FAILED: live server reports $LIVE_SHA but we deployed $GIT_SHA." >&2
  echo "         The deploy did not take effect. Investigate before assuming it worked." >&2
  exit 1
fi
DOCS_PUBLIC="$(printf '%s' "$LIVE" | sed -n 's/.*"api_docs_public" *: *\([a-z]*\).*/\1/p')"
echo "[deploy] live commit  $LIVE_SHA  (matches)"
echo "[deploy] api docs public: ${DOCS_PUBLIC:-unknown}"
[ "$DOCS_PUBLIC" = "true" ] && echo "[deploy] NOTE: interactive API docs are PUBLIC on this deployment (NB_API_DOCS=1)."
echo "[deploy] done"
