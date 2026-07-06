#!/usr/bin/env bash
# Admin tool: build a personalized developer kit zip.
# Usage: bash make-dev-kit.sh "Ravi" 100.101.102.103 [MIC_GAIN]
set -euo pipefail
NAME="${1:?usage: make-dev-kit.sh <name> <bridge-tailscale-ip> [mic_gain]}"
BRIDGE="${2:?bridge tailscale ip required}"
GAIN="${3:-8}"
D="$(cd "$(dirname "$0")" && pwd)"
R="$(dirname "$D")"
OUT="$D/kits/${NAME}-replikam-kit"
rm -rf "$OUT"; mkdir -p "$OUT/mac" "$OUT/windows"
# personalized conf
cat > "$OUT/developer.conf" <<CONF
DEV_NAME=$NAME
BRIDGE=$BRIDGE
MIC_GAIN_DB=$GAIN
FPS=20
CAMERA=
MIC=
CONF
# mac kit = proven scripts + wrappers
cp "$R/mac/go-live.sh" "$R/mac/stop-live.sh" "$R/mac/mac-stream.sh" "$R/mac/mac-return-listen.sh" "$OUT/mac/"
cp "$D/mac/GO-LIVE.command" "$D/mac/STOP.command" "$D/mac/CHECKS.command" "$OUT/mac/"
cp "$OUT/developer.conf" "$OUT/mac/"
# windows kit
cp "$D/windows/go-live.ps1" "$D/windows/stop-live.ps1" "$OUT/windows/"
cp "$OUT/developer.conf" "$OUT/windows/"
cp "$D/ONBOARDING.md" "$OUT/"
chmod +x "$OUT"/mac/*.command "$OUT"/mac/*.sh
(cd "$D/kits" && zip -qr "${NAME}-replikam-kit.zip" "$(basename "$OUT")")
echo "📦 kit ready: $D/kits/${NAME}-replikam-kit.zip  (send to $NAME + their tailscale key)"
