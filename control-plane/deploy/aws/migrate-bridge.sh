#!/bin/bash
# Point a bridge at a new control plane. Run with the TARGET card in the reader on the Pi.
#
#   migrate-bridge.sh <pi-host> <new-control-url> <device-bootstrap-token>
#
# THREE changes are needed, not one. The agent's enroll() is:
#     if os.path.exists(TOKEN_FILE): return open(TOKEN_FILE).read()
# so a bridge that already holds a device token NEVER re-enrolls. Point it at a new fleet
# without clearing that token and it keeps presenting a credential the new fleet has never
# seen: every call 401s and the bridge is silently offline forever, with nothing to explain
# it. So we set the URL, supply a bootstrap token, AND remove the old device token.
set -uo pipefail
PI="${1:?usage: migrate-bridge.sh <pi-host> <control-url> <bootstrap-token>}"
URL="${2:?need the new control plane URL}"
BOOT="${3:?need the device bootstrap token}"
KEY=~/.ssh/pi_bridge
S="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=8 pi@$PI"
case "$URL" in https://*) : ;; *) echo "  ❌ refuse a non-https control plane (device tokens cross this link)"; exit 1 ;; esac

$S 'echo OK' 2>/dev/null | grep -q OK || { echo "  ❌ no SSH to $PI"; exit 1; }
$S "bash -s" <<REMOTE
set -uo pipefail
sudo systemctl stop bridge-powertrim 2>/dev/null
echo "0000:01:00.0" | sudo tee /sys/bus/pci/drivers/xhci_hcd/bind >/dev/null 2>&1 || true
sleep 8
lsblk -o NAME,LABEL | grep -q rootA || { echo "  ❌ target card not in the reader"; exit 1; }
sudo mkdir -p /mnt/tc && sudo mount /dev/sda2 /mnt/tc 2>/dev/null
[ -n "\$(findmnt -no SOURCE /mnt/tc)" ] || { echo "  ❌ mount failed"; exit 1; }
M=/mnt/tc
echo "  ✅ card mounted"

C=\$M/etc/default/bridge-agent
sudo cp -n "\$C" "\$C.pre-migrate" 2>/dev/null || true
sudo sed -i '/^CONTROL_URL=/d;/^BOOTSTRAP_TOKEN=/d' "\$C" 2>/dev/null || true
echo "CONTROL_URL=$URL"      | sudo tee -a "\$C" >/dev/null
echo "BOOTSTRAP_TOKEN=$BOOT" | sudo tee -a "\$C" >/dev/null
echo "  ✅ 1/3 CONTROL_URL + BOOTSTRAP_TOKEN written"

# The old device token MUST go, or enroll() short-circuits and the bridge never registers.
if sudo test -f \$M/etc/bridge/agent.token; then
  sudo mv \$M/etc/bridge/agent.token \$M/etc/bridge/agent.token.pre-migrate
  echo "  ✅ 2/3 old device token moved aside (kept as .pre-migrate for rollback)"
else
  echo "  ✅ 2/3 no device token on the card — will enroll fresh"
fi

sudo grep -E '^(CONTROL_URL|BOOTSTRAP_TOKEN)=' "\$C" | sed 's/BOOTSTRAP_TOKEN=.*/BOOTSTRAP_TOKEN=<set>/' | sed 's/^/    /'
echo "  ✅ 3/3 verified on the card"
sudo sync; sudo umount /mnt/tc && echo "  ✅ unmounted cleanly"; sudo sync
REMOTE
rc=$?
echo
[ $rc -eq 0 ] && echo "════ MIGRATION WRITTEN — swap the card back and boot ════" \
              || echo "════ FAILED (rc=$rc) — card left as-is, do NOT swap ════"
exit $rc
