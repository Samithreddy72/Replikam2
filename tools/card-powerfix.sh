#!/bin/bash
# CARD SURGERY — undervoltage mitigation + the D1 quarantine fix.
#
# Run with the MAIN card booted in the Pi and the TARGET card in a USB reader on that Pi.
#
#     bash card-powerfix.sh <pi-host>
#
# WHAT IT DOES, AND WHY EACH PIECE IS NEEDED
#
#   1. bridge-powertrim — present in the repo and enabled by the CI image, but NEVER
#      installed on hand-built cards. It powers down the unused USB-A host controller
#      (~100-200mA, the single biggest saver), HDMI, the LEDs and eth0, and caps the CPU.
#
#   2. config.txt power settings — powertrim has ExecStartPre=sleep 12, so it lands at ~12s
#      while the observed dips are at 13.7 / 21.8 / 31.9s: all cores ramping, the USB gadget
#      enumerating, the Wi-Fi radio coming up. It cannot cover its own boot window. Only the
#      firmware config can, so arm_boost/gpu_mem/audio/LEDs go there.
#
#   3. bridge-run.sh — the D1 fix. Start stamps were wall-clock, and pre-NTP every boot
#      stamps the same value, so three POWER CYCLES looked like a crash loop and quarantined
#      a healthy override. That already happened once tonight. Since we are about to ask this
#      bridge to power cycle repeatedly while testing, shipping it unfixed guarantees a repeat.
#
# Nothing here is destructive: every original is kept with a .pre-powerfix suffix.
set -uo pipefail
PI="${1:?usage: card-powerfix.sh <pi-host>}"
KEY=~/.ssh/pi_bridge
R=~/netbridge/replikam2-ci
S="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=8 pi@$PI"
die(){ echo "  ❌ $*" >&2; exit 1; }

echo "════ staging ════"
for f in "$R/pi/scripts/bridge-powertrim.sh" "$R/pi/systemd/bridge-powertrim.service" \
         "$R/pi/scripts/bridge-run.sh"; do
  [ -f "$f" ] || die "missing $f"
done
bash -n "$R/pi/scripts/bridge-powertrim.sh" || die "powertrim syntax"
bash -n "$R/pi/scripts/bridge-run.sh"       || die "loader syntax"
echo "  ✅ payloads pass syntax checks locally"

$S 'echo OK' 2>/dev/null | grep -q OK || die "no SSH to $PI"
scp -i "$KEY" -o StrictHostKeyChecking=no \
  "$R/pi/scripts/bridge-powertrim.sh" "$R/pi/systemd/bridge-powertrim.service" \
  "$R/pi/scripts/bridge-run.sh" "pi@$PI:/tmp/" >/dev/null || die "scp failed"
echo "  ✅ payloads on the Pi"

$S 'bash -s' <<'REMOTE'
set -uo pipefail
# The reader hangs off the USB-A host controller that powertrim powers down — so if this Pi
# has already trimmed itself, the card is invisible until we bind the controller back.
sudo systemctl stop bridge-powertrim 2>/dev/null
echo "0000:01:00.0" | sudo tee /sys/bus/pci/drivers/xhci_hcd/bind >/dev/null 2>&1 || true
sleep 8

echo "── partitions on the reader ──"
lsblk -o NAME,LABEL,SIZE /dev/sda 2>/dev/null | sed 's/^/    /'
lsblk -o NAME,LABEL /dev/sda 2>/dev/null | grep -qi root || { echo "  ❌ target card not visible"; exit 1; }

sudo mkdir -p /mnt/tcboot /mnt/tcroot
sudo umount /mnt/tcboot /mnt/tcroot 2>/dev/null || true
sudo mount /dev/sda1 /mnt/tcboot 2>/dev/null || { echo "  ❌ cannot mount boot (sda1)"; exit 1; }
sudo mount /dev/sda2 /mnt/tcroot 2>/dev/null || { echo "  ❌ cannot mount root (sda2)"; sudo umount /mnt/tcboot; exit 1; }
[ -f /mnt/tcboot/config.txt ] || { echo "  ❌ no config.txt on sda1 — wrong partition"; sudo umount /mnt/tcboot /mnt/tcroot; exit 1; }
echo "  ✅ boot + root mounted"

# ---- 1. powertrim script + unit ----
sudo install -m 0755 /tmp/bridge-powertrim.sh /mnt/tcroot/usr/local/bin/bridge-powertrim.sh
sudo install -m 0644 /tmp/bridge-powertrim.service /mnt/tcroot/etc/systemd/system/bridge-powertrim.service
# Enable it by hand: systemctl cannot enable into an offline root reliably, and the unit's
# [Install] section is just WantedBy=multi-user.target — which IS this symlink.
sudo mkdir -p /mnt/tcroot/etc/systemd/system/multi-user.target.wants
sudo ln -sf /etc/systemd/system/bridge-powertrim.service \
            /mnt/tcroot/etc/systemd/system/multi-user.target.wants/bridge-powertrim.service
[ -L /mnt/tcroot/etc/systemd/system/multi-user.target.wants/bridge-powertrim.service ] \
  && echo "  ✅ 1/3 bridge-powertrim installed AND enabled" || { echo "  ❌ enable symlink failed"; exit 1; }

# ---- 2. config.txt: the boot-window spikes powertrim cannot reach ----
CFG=/mnt/tcboot/config.txt
sudo cp -n "$CFG" "$CFG.pre-powerfix" 2>/dev/null || true
if grep -q "NetBridge power" "$CFG"; then
  echo "  ✅ 2/3 config.txt already carries the power block (skipped)"
else
  sudo tee -a "$CFG" >/dev/null <<'EOF'

# --- NetBridge power: cut the current SPIKES ---
# 22 undervoltage events/boot were logged at 13.7/21.8/31.9s — cores ramping, USB gadget
# enumerating, Wi-Fi coming up. bridge-powertrim starts at ~12s and cannot cover that window.
arm_boost=0
gpu_mem=16
dtparam=audio=off
dtparam=act_led_trigger=none
dtparam=act_led_activelow=off
dtparam=pwr_led_trigger=none
dtparam=pwr_led_activelow=off
EOF
  grep -q "arm_boost=0" "$CFG" && echo "  ✅ 2/3 config.txt power block appended" \
                               || { echo "  ❌ config.txt write failed"; exit 1; }
fi

# ---- 3. the D1 loader fix ----
sudo cp -n /mnt/tcroot/usr/local/bin/bridge-run.sh \
           /mnt/tcroot/usr/local/bin/bridge-run.sh.pre-powerfix 2>/dev/null || true
sudo install -m 0755 /tmp/bridge-run.sh /mnt/tcroot/usr/local/bin/bridge-run.sh
sudo bash -n /mnt/tcroot/usr/local/bin/bridge-run.sh || { echo "  ❌ loader syntax on card"; exit 1; }
sudo grep -q 'BRIDGE_RUN_BOOTID_SRC' /mnt/tcroot/usr/local/bin/bridge-run.sh \
  && echo "  ✅ 3/3 bridge-run.sh carries the D1 boot-id fix" \
  || { echo "  ❌ D1 marker missing"; exit 1; }

# ---- sanity: don't leave a card that cannot boot ----
sudo test -x /mnt/tcroot/usr/local/bin/bridge-powertrim.sh || { echo "  ❌ powertrim not executable"; exit 1; }
grep -q 'dtoverlay=dwc2,dr_mode=peripheral' "$CFG" || { echo "  ❌ dwc2 line vanished from config.txt!"; exit 1; }
echo "  ✅ dwc2 gadget line still present (card will still enumerate)"

sudo sync
sudo umount /mnt/tcboot /mnt/tcroot && echo "  ✅ unmounted cleanly"
sudo sync
rm -f /tmp/bridge-powertrim.sh /tmp/bridge-powertrim.service /tmp/bridge-run.sh
REMOTE
rc=$?
echo
[ $rc -eq 0 ] && echo "════ DONE — swap the card back and boot ════" \
              || echo "════ FAILED (rc=$rc) — do NOT swap; card left as-is ════"
exit $rc
