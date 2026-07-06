#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  RepliKam SD FACTORY — stamp fleet cards from the golden image (macOS)
#
#  Usage:   bash make-card.sh <NN> [/dev/diskX]
#  Example: bash make-card.sh 7          → makes bridge-007 (auto-detects the SD)
#
#  Per card: flashes factory/replikam-golden.img + injects that unit's
#  provisioning conf (hostname bridge-0NN + fleet secrets from fleet.conf).
#  On first boot the card joins the tailnet, enrolls with the control plane,
#  and appears in the admin panel — zero further touch.
#
#  One-time prereqs:
#    1. Create the golden image (see GOLDEN-IMAGE.md) at factory/replikam-golden.img
#    2. Copy fleet.conf.example → fleet.conf and fill the secrets (never commit it)
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
NN="${1:?usage: make-card.sh <NN> [/dev/diskX]   e.g. make-card.sh 7}"
NN=$(printf "%03d" "$NN")
D="$(cd "$(dirname "$0")" && pwd)"
IMG="$D/replikam-golden.img"
CONF="$D/fleet.conf"
[ -f "$IMG" ]  || { echo "❌ golden image missing: $IMG (see GOLDEN-IMAGE.md)"; exit 1; }
[ -f "$CONF" ] || { echo "❌ fleet.conf missing: copy fleet.conf.example and fill it"; exit 1; }
. "$CONF"

# ── find the SD card ────────────────────────────────────────────────────────
DISK="${2:-}"
if [ -z "$DISK" ]; then
  DISK=$(diskutil list external physical 2>/dev/null | awk '/^\/dev\/disk/{print $1}' | head -1)
fi
[ -n "$DISK" ] || { echo "❌ no external disk found — insert the SD card"; exit 1; }
echo "══ TARGET CARD ══"
diskutil info "$DISK" | grep -E "Device Node|Media Name|Disk Size" | sed 's/^/  /'
echo ""
read -p "⚠️  ERASE $DISK and make it bridge-$NN? Type the disk name to confirm ($DISK): " OK
[ "$OK" = "$DISK" ] || { echo "aborted"; exit 1; }

# ── flash ───────────────────────────────────────────────────────────────────
echo "══ flashing golden image (~5-8 min) ══"
diskutil unmountDisk "$DISK"
RDISK="${DISK/disk/rdisk}"
sudo dd if="$IMG" of="$RDISK" bs=8m status=progress
sync
echo "  ✓ flashed"

# ── inject this unit's identity ─────────────────────────────────────────────
echo "══ stamping bridge-$NN ══"
sleep 3; diskutil mountDisk "$DISK" >/dev/null 2>&1 || true; sleep 3
BOOT=$(ls -d /Volumes/bootfs /Volumes/boot 2>/dev/null | head -1)
[ -n "$BOOT" ] || { echo "❌ boot partition didn't mount — remove/reinsert the card, then run: bash inject-only.sh $NN"; exit 1; }
TMP=$(mktemp)
cat > "$TMP" <<EOF
TS_AUTHKEY=$TS_AUTHKEY
TS_HOSTNAME=bridge-$NN
CONTROL_URL=$CONTROL_URL
BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
BRIDGE_VERSION=${BRIDGE_VERSION:-2.0.0}
EOF
install -m 600 "$TMP" "$BOOT/bridge-provision.conf"
rm -f "$TMP"
echo "  ✓ provision conf written (first boot consumes + shreds it)"
diskutil eject "$DISK"
echo ""
echo "🏭 bridge-$NN card READY — insert into a Pi, power on, and it appears"
echo "   in the admin panel within ~2 minutes. Insert the next card and re-run."
