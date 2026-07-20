#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  RepliKam SD FACTORY — stamp fleet cards from the golden image (macOS)
#
#  Usage:   bash make-card.sh <NN> [/dev/diskX]
#  Example: bash make-card.sh 7          → makes bridge-007 (auto-detects the SD)
#
#  Per card: flashes factory/replikam-golden.img + injects that unit's
#  provisioning conf (hostname bridge-0NN + fleet bootstrap from fleet.conf).
#  KEY-AT-CLAIM (M10): the card carries NO tailnet key. On first boot it enrolls
#  with the control plane over its public HTTPS URL using only a bootstrap token,
#  then the admin claims it and the tailnet key is delivered once. So a lost card
#  is not a live tailnet key.
#  ⚠️ CONTROL_URL in fleet.conf MUST be publicly reachable — a tailnet-only URL
#     deadlocks a keyless card (can't reach the control plane to get on the tailnet).
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
# No TS_AUTHKEY on the card (key-at-claim). Only identity hint + enrollment
# bootstrap. TS_HOSTNAME is kept as a friendly default the agent can pass at the
# claim-time `tailscale up`; it is not a secret.
TMP=$(mktemp)
cat > "$TMP" <<EOF
TS_HOSTNAME=bridge-$NN
CONTROL_URL=$CONTROL_URL
BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
BRIDGE_VERSION=${BRIDGE_VERSION:-2.0.0}
EOF
install -m 600 "$TMP" "$BOOT/bridge-provision.conf"
rm -f "$TMP"
case "$CONTROL_URL" in
  https://*) : ;;
  *) echo "  ⚠️  CONTROL_URL is not https:// — a keyless card can only enroll over a"
     echo "      publicly reachable HTTPS control plane. A tailnet/localhost URL will"
     echo "      leave this card unable to enroll. (See fleet.conf.example.)" ;;
esac
echo "  ✓ provision conf written — NO tailnet key on the card (key-at-claim)"
diskutil eject "$DISK"
echo ""
echo "🏭 bridge-$NN card READY — insert into a Pi, power on, and it appears"
echo "   in the admin panel as UNCLAIMED within ~2 minutes. Claim it there to"
echo "   deliver its tailnet key and bring it online. Insert the next card and re-run."
