#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  RepliKam2 — ONE-CLICK SETUP:  freshly flashed SD card ➜ working bridge
#
#  Usage:   bash setup.sh <pi-address>
#  Example: bash setup.sh bridge-001.local
#
#  Before running, complete "Step 1 — Flash" in docs/SETUP-GUIDE.md
#  (Raspberry Pi Imager with hostname/SSH/Wi-Fi preset — 5 minutes).
#
#  What this script does, fully automatically (~10-15 min):
#    • installs all packages (GStreamer, v4l2loopback, build tools)
#    • installs the pinned kernel 6.12.93  ← CRITICAL: kernel 6.18 hard-freezes
#      the Pi when a laptop connects to the USB port (dwc2 driver bug)
#    • configures the USB gadget (webcam + mic + speaker identity)
#    • deploys every tested script, service, and binary
#    • enables the self-healing stack (Wi-Fi guardian, watchdogs, jitter sentry)
#    • reboots and verifies itself
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
T="${1:?usage: bash setup.sh <pi-address>   e.g. bash setup.sh bridge-001.local}"
D="$(cd "$(dirname "$0")" && pwd)"
KEY=~/.ssh/pi_bridge
S(){ ssh -i $KEY -o StrictHostKeyChecking=accept-new pi@"$T" "$@"; }
bar(){ printf '\n═══ %s ═══\n' "$1"; }

bar "0/8  SSH key (enter the Pi's password once — never again after this)"
[ -f $KEY ] || ssh-keygen -t ed25519 -f $KEY -N "" >/dev/null
ssh-copy-id -i $KEY.pub -o StrictHostKeyChecking=accept-new pi@"$T" 2>/dev/null || true
S 'echo "  ✓ connected to $(hostname) ($(uname -m))"' || { echo "❌ cannot reach pi@$T — check the address and that the Pi finished booting (wait 90s after power-on)"; exit 1; }

bar "1/8  pushing files (~45MB)"
S 'mkdir -p /home/pi/replikam2'
scp -q -r -i $KEY "$D/pi" "$D/restore" "$D/sources" pi@"$T":/home/pi/replikam2/
echo "  ✓ pushed"

bar "2/8  packages (needs internet on the Pi; 5-10 min — grab a coffee)"
S 'sudo apt-get update -qq && sudo apt-get install -y -qq \
   gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
   gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-alsa \
   v4l2loopback-dkms v4l2loopback-utils v4l-utils alsa-utils python3 python3-pil \
   git meson ninja-build build-essential 2>&1 | tail -1'
echo "  ✓ packages ready"

bar "3/8  kernel 6.12.93 (the freeze-proof kernel)"
S 'cd /home/pi/replikam2/restore/kernel
   sudo apt-get install -y -qq gcc-12 cpp-12 gcc-12-base libgcc-12-dev 2>/dev/null
   sudo mv /etc/kernel/postinst.d/dkms /tmp/h1 2>/dev/null; sudo mv /etc/kernel/header_postinst.d/dkms /tmp/h2 2>/dev/null
   sudo dpkg -i linux-kbuild-*.deb linux-image-*.deb linux-headers-*common*.deb linux-headers-*rpi-v8_*.deb >/dev/null 2>&1
   sudo mv /tmp/h1 /etc/kernel/postinst.d/dkms 2>/dev/null; sudo mv /tmp/h2 /etc/kernel/header_postinst.d/dkms 2>/dev/null
   sudo dkms autoinstall -k 6.12.93+rpt-rpi-v8 >/dev/null 2>&1
   [ -f /boot/initrd.img-6.12.93+rpt-rpi-v8 ] || sudo update-initramfs -c -k 6.12.93+rpt-rpi-v8 >/dev/null 2>&1
   sudo cp /boot/vmlinuz-6.12.93+rpt-rpi-v8 /boot/firmware/kernel612.img
   sudo cp /boot/initrd.img-6.12.93+rpt-rpi-v8 /boot/firmware/initramfs612
   sudo apt-mark hold linux-image-rpi-v8 linux-headers-rpi-v8 >/dev/null 2>&1
   echo "  ✓ kernel staged + upgrades frozen"'

bar "4/8  boot configuration (USB gadget + kernel pin + power headroom)"
S 'C=/boot/firmware/config.txt
   grep -q "dtoverlay=dwc2" $C || printf "\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\ndtoverlay=disable-bt\narm_freq=900\ngpu_freq=300\ninitial_turbo=0\nkernel=kernel612.img\ninitramfs initramfs612 followkernel\n" | sudo tee -a $C >/dev/null
   grep -q "modules-load=dwc2" /boot/firmware/cmdline.txt || sudo sed -i "s/rootwait/rootwait modules-load=dwc2/" /boot/firmware/cmdline.txt
   echo "  ✓ boot config written"'

bar "5/8  deploying the tested bridge (scripts, services, binaries)"
S 'cd /home/pi/replikam2
   sudo cp pi/scripts/* /usr/local/bin/ && sudo mv /usr/local/bin/uvc-raw-setup.sh /home/pi/uvc-raw-setup.sh
   sudo cp -r pi/systemd/* /etc/systemd/system/
   sudo cp restore/binaries/uvc-gadget restore/binaries/uvc-gadget-wlhe /usr/local/bin/
   sudo mkdir -p /usr/local/lib/aarch64-linux-gnu
   sudo cp restore/binaries/libuvcgadget.so.0.4.0 /usr/local/lib/aarch64-linux-gnu/
   sudo ln -sf /usr/local/lib/aarch64-linux-gnu/libuvcgadget.so.0.4.0 /usr/local/lib/aarch64-linux-gnu/libuvcgadget.so.0
   sudo ldconfig
   sudo cp pi/configs/v4l2loopback.conf /etc/modprobe.d/
   sudo mkdir -p /etc/systemd/journald.conf.d /etc/systemd/system.conf.d /etc/NetworkManager/conf.d
   sudo cp pi/configs/size-cap.conf /etc/systemd/journald.conf.d/ 2>/dev/null
   sudo cp pi/configs/kit-watchdog.conf /etc/systemd/system.conf.d/99-watchdog.conf 2>/dev/null
   sudo cp pi/configs/wifi-powersave-off.conf pi/configs/no-mac-rand.conf /etc/NetworkManager/conf.d/ 2>/dev/null
   sudo chmod +x /usr/local/bin/bridge* /usr/local/bin/wifi-guardian.sh /usr/local/bin/flight-recorder.sh /usr/local/bin/jitter-sentry.sh /home/pi/uvc-raw-setup.sh 2>/dev/null
   tar xzf sources/patched-uvc-gadget-sources.tgz -C /home/pi/ 2>/dev/null
   echo "  ✓ deployed"'

bar "6/8  remote access (Tailscale — reach the Pi from anywhere)"
S 'command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sudo sh >/dev/null 2>&1
   echo "  ✓ installed — AFTER SETUP run once:  ssh -i ~/.ssh/pi_bridge pi@'"$T"' sudo tailscale up"'

bar "7/8  enabling the self-healing stack"
S 'sudo systemctl daemon-reload
   for u in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio wifi-guardian bridge-powertrim flight-recorder jitter-sentry bridge-supervisor bridge-watchdog.timer bridge-web bridge-wifi-portal bridge-idle-frame bridge-idle-frame.timer gadget-clean-detach bridge-agent.timer; do sudo systemctl enable $u >/dev/null 2>&1; done
   sudo systemctl disable bridge-testpattern >/dev/null 2>&1   # agent.timer stays ON: fresh bridges must enroll with the fleet
   echo "  ✓ 14 services armed"'

bar "8/8  reboot + self-verification"
S 'sudo reboot' 2>/dev/null || true
echo "  rebooting into the bridge (~90s)..."
sleep 50
for i in $(seq 1 20); do S 'true' 2>/dev/null && break; sleep 5; done
S 'K=$(uname -r); A=$(systemctl is-active bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio wifi-guardian jitter-sentry | grep -c active)
   echo "  kernel:   $K $([ "${K%%+*}" = "6.12.93" ] && echo "✅" || echo "❌ expected 6.12.93")"
   echo "  services: $A/7 active $([ "$A" = 7 ] && echo "✅")"
   echo "  camera:   $([ -e /dev/video40 ] && echo "✅ ready") | gadget: $(cat /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null)"' \
 && printf '\n🏆 SETUP COMPLETE!\n   1) once:  ssh -i ~/.ssh/pi_bridge pi@%s sudo tailscale up\n   2) plug the client laptop into the Pi USB-C\n   3) on the Mac:  bash mac/go-live.sh\n   4) in the meeting: camera=UVC Camera, mic/speaker=Source/Sink\n' "$T" \
 || printf '\n⚠ Pi still booting — wait 2 min then verify:  ssh -i ~/.ssh/pi_bridge pi@%s uname -r\n' "$T"
