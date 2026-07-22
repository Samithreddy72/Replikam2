#!/bin/bash
# ci-build-image.sh — runs INSIDE pguyot/arm-runner-action's emulated Raspberry Pi OS
# (arm64) rootfs, as root. Replays the Replikam2 setup.sh provisioning (stages 2-7) minus
# the SSH wrapper, the final reboot self-test, and the Tailscale *join*, then GENERALIZES
# the image (strips every per-device secret) so CI can publish a signed, secret-free
# factory image. This replaces the manual factory/GOLDEN-IMAGE.md process.
#
# Env: REPLIKAM_REPO (repo checkout inside the image, default /opt/replikam2),
#      IMAGE_VERSION (stamped into /etc/bridge/version; the flashed BRIDGE_VERSION
#      re-stamps it at first boot).
set -uo pipefail
REPO="${REPLIKAM_REPO:-/opt/replikam2}"
IMAGE_VERSION="${IMAGE_VERSION:-2.0.0-dev}"
KVER=6.12.93+rpt-rpi-v8
cd "$REPO" || { echo "FATAL: repo not found at $REPO"; exit 1; }
log(){ echo "== ci-build: $* =="; }
export DEBIAN_FRONTEND=noninteractive

# ---------------- Stage 2: packages ----------------
log "apt packages"
apt-get update
apt-get install -y --no-install-recommends \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-alsa \
  v4l2loopback-dkms v4l2loopback-utils v4l-utils alsa-utils \
  python3 python3-pil git meson ninja-build build-essential \
  gcc-12 cpp-12 gcc-12-base libgcc-12-dev \
  network-manager rsync ca-certificates curl

# ---------------- Stage 3: pinned kernel 6.12.93 (dwc2 freeze fix) ----------------
log "pinned kernel $KVER"
# Move the DKMS kernel hooks aside so dpkg -i doesn't try to rebuild DKMS modules
# against the build-host kernel mid-install (the setup.sh "dkms dance").
mv /etc/kernel/postinst.d/dkms        /tmp/dkms.postinst.bak 2>/dev/null || true
mv /etc/kernel/header_postinst.d/dkms /tmp/dkms.header.bak   2>/dev/null || true
dpkg -i restore/kernel/linux-kbuild-*.deb restore/kernel/linux-image-*.deb \
        restore/kernel/linux-headers-*common*.deb restore/kernel/linux-headers-*rpi-v8_*.deb
mv /tmp/dkms.postinst.bak /etc/kernel/postinst.d/dkms        2>/dev/null || true
mv /tmp/dkms.header.bak   /etc/kernel/header_postinst.d/dkms 2>/dev/null || true
dkms autoinstall -k "$KVER" || echo "WARN: dkms autoinstall non-zero (continuing)"
update-initramfs -c -k "$KVER"
cp -v /boot/vmlinuz-$KVER    /boot/firmware/kernel612.img
cp -v /boot/initrd.img-$KVER /boot/firmware/initramfs612
apt-mark hold linux-image-rpi-v8 linux-headers-rpi-v8 || true

# Overlay-capable initramfs for READ-ONLY ROOT. This single-partition image stays
# writable (its config.txt uses the plain initramfs612 above); the OVERLAY initramfs is
# only *engaged* by the flashable full-disk image (factory/build-disk-image.sh), which is
# safe because that image has a separate /data to hold writes. We build it here so p1
# carries it. Install overlayroot, rebuild the initramfs (now with the overlayroot hook),
# ship it as initramfs612-overlay, leaving initramfs612 overlay-free. Mirrors the device.
apt-get install -y --no-install-recommends overlayroot
update-initramfs -u -k "$KVER"
cp -v /boot/initrd.img-$KVER /boot/firmware/initramfs612-overlay

# ---------------- Stage 4: boot config ----------------
log "boot config (dwc2 peripheral + pinned kernel)"
CFG=/boot/firmware/config.txt; CMD=/boot/firmware/cmdline.txt
if ! grep -q 'dtoverlay=dwc2,dr_mode=peripheral' "$CFG"; then
cat >> "$CFG" <<'EOF'

# --- NetBridge ---
dtoverlay=dwc2,dr_mode=peripheral
dtoverlay=disable-bt
arm_freq=900
kernel=kernel612.img
initramfs initramfs612 followkernel
EOF
fi
grep -q 'modules-load=dwc2' "$CMD" || sed -i '1 s/$/ modules-load=dwc2/' "$CMD"

# ---------------- Stage 5: deploy the bridge payload ----------------
log "deploy scripts / units / uvc binaries / configs"
install -d /usr/local/bin /usr/local/lib/aarch64-linux-gnu /home/pi
for f in pi/scripts/*; do
  b=$(basename "$f")
  if [ "$b" = "uvc-raw-setup.sh" ]; then install -m 0755 "$f" /home/pi/; else install -m 0755 "$f" /usr/local/bin/; fi
done
install -m 0644 pi/systemd/*.service pi/systemd/*.timer /etc/systemd/system/
install -m 0755 restore/binaries/uvc-gadget /usr/local/bin/
install -m 0755 restore/binaries/libuvcgadget.so.0.4.0 /usr/local/lib/aarch64-linux-gnu/
ln -sf libuvcgadget.so.0.4.0 /usr/local/lib/aarch64-linux-gnu/libuvcgadget.so.0
ldconfig
install -d /etc/modprobe.d /etc/systemd/journald.conf.d /etc/systemd/system.conf.d /etc/NetworkManager/conf.d
install -m 0644 pi/configs/v4l2loopback.conf       /etc/modprobe.d/
install -m 0644 pi/configs/size-cap.conf           /etc/systemd/journald.conf.d/
install -m 0644 pi/configs/kit-watchdog.conf       /etc/systemd/system.conf.d/99-watchdog.conf
install -m 0644 pi/configs/wifi-powersave-off.conf pi/configs/no-mac-rand.conf /etc/NetworkManager/conf.d/
tar xzf sources/patched-uvc-gadget-sources.tgz -C /home/pi 2>/dev/null || true
chown -R 1000:1000 /home/pi 2>/dev/null || true

# ---------------- Stage 6: Tailscale daemon (installed, NOT joined) ----------------
log "tailscale daemon (key-at-claim: never joined in the image)"
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable tailscaled || true

# ---------------- Stage 7: enable units (exact list from setup.sh + firstboot/hostkeys) ----------------
log "enable units"
for u in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio \
         wifi-guardian bridge-powertrim flight-recorder jitter-sentry bridge-supervisor \
         bridge-watchdog.timer bridge-web bridge-wifi-portal bridge-idle-frame \
         bridge-idle-frame.timer gadget-clean-detach bridge-agent.timer \
         bridge-ab-healthcheck \
         bridge-firstboot bridge-regen-hostkeys; do
  systemctl enable "$u" 2>/dev/null || echo "WARN: could not enable $u"
done
systemctl disable bridge-testpattern 2>/dev/null || true

# ---------------- GENERALIZE: strip ALL per-device identity (secret-free) ----------------
log "generalize -> secret-free"
rm -f  /etc/bridge/agent.token /etc/bridge/setup-wifi-pass /etc/bridge/version
rm -f  /etc/bridge/bridge-provision.conf /boot/firmware/bridge-provision.conf
rm -rf /var/lib/tailscale/*                       # no tailnet identity on the card
rm -f  /etc/default/bridge-agent                  # CONTROL_URL + bootstrap token arrive at flash / first boot
rm -f  /etc/NetworkManager/system-connections/*   # no Wi-Fi PSKs baked in (onboard via setup portal)
rm -f  /etc/ssh/ssh_host_*                         # regenerated per-device by bridge-regen-hostkeys on first boot
# fleet-brain is NOT part of the bridge image (it is the separate control plane) — nothing to strip here.
install -d /etc/bridge
echo "$IMAGE_VERSION" > /etc/bridge/version        # bridge-firstboot re-stamps with the flashed BRIDGE_VERSION

# ---------------- Verify no obvious secrets survived ----------------
log "secret sweep"
LEAK=0
grep -rIlE 'tskey-|BEGIN OPENSSH PRIVATE KEY|psk=.+|ADMIN_API_KEY=.+' \
   /etc/NetworkManager /etc/default /var/lib/tailscale /etc/ssh /etc/bridge 2>/dev/null | while read -r f; do
   echo "  LEAK: $f"; LEAK=1
done
echo "== ci-build: done (version $IMAGE_VERSION) =="
