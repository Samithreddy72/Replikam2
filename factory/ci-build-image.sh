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
WC_VER=v4.11.84            # balena wifi-connect (setup portal)
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
  network-manager dnsmasq-base rsync ca-certificates curl \
  cloud-guest-utils parted e2fsprogs \
  rfkill iw \
  fonts-dejavu-core
# fonts-dejavu-core: render-idle-frame.py loads DejaVuSans.ttf by absolute path. Raspberry Pi
# OS Lite ships NO truetype fonts, so the renderer raised FileNotFoundError on every run and
# the in-camera status card was never re-rendered on the device - the meeting laptop showed
# the frame baked in at BUILD time (CI has no wifi and no USB host => all three ticks red)
# while the bridge was actually online and enumerated. Found 2026-07-24 on the client laptop.

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
# Locate the REAL boot partition = the directory where config.txt actually lives. arm-runner
# does NOT reliably mount it at /boot/firmware, so the previous builds wrote the pinned kernel
# to a path that never reached the card. Co-locating with config.txt guarantees it lands on the
# boot filesystem that gets flashed. The probe below makes the layout explicit in the log.
echo "== ci-build: BOOT LAYOUT PROBE =="
findmnt -o TARGET,SOURCE,FSTYPE 2>/dev/null | grep -iE 'boot|firmware|[[:space:]]/[[:space:]]' || true
echo "  config.txt found at: $(find /boot -maxdepth 3 -name config.txt 2>/dev/null | tr '\n' ' ')"
echo "  /boot: $(ls /boot 2>/dev/null | tr '\n' ' ')"
echo "  /boot/firmware: $(ls /boot/firmware 2>/dev/null | tr '\n' ' ')"
BOOTDIR=""
for d in /boot/firmware /boot; do [ -f "$d/config.txt" ] && { BOOTDIR="$d"; break; }; done
[ -n "$BOOTDIR" ] || BOOTDIR="$(dirname "$(find /boot -maxdepth 3 -name config.txt 2>/dev/null | head -1)")"
[ -f "$BOOTDIR/config.txt" ] || { echo "FATAL: cannot locate config.txt under /boot"; find /boot -maxdepth 3 2>/dev/null | head -60; exit 1; }
export BOOTDIR
echo "== ci-build: BOOTDIR = $BOOTDIR =="
cp -v /boot/vmlinuz-$KVER    "$BOOTDIR/kernel612.img"
cp -v /boot/initrd.img-$KVER "$BOOTDIR/initramfs612"
apt-mark hold linux-image-rpi-v8 linux-headers-rpi-v8 || true

# Overlay-capable initramfs for READ-ONLY ROOT. This single-partition image stays
# writable (its config.txt uses the plain initramfs612 above); the OVERLAY initramfs is
# only *engaged* by the flashable full-disk image (factory/build-disk-image.sh), which is
# safe because that image has a separate /data to hold writes. We build it here so p1
# carries it. Install overlayroot, rebuild the initramfs (now with the overlayroot hook),
# ship it as initramfs612-overlay, leaving initramfs612 overlay-free. Mirrors the device.
apt-get install -y --no-install-recommends overlayroot
update-initramfs -u -k "$KVER"
cp -v /boot/initrd.img-$KVER "$BOOTDIR/initramfs612-overlay"

# ---------------- Stage 4: boot config ----------------
log "boot config (dwc2 peripheral + pinned kernel)"
CFG="$BOOTDIR/config.txt"; CMD="$BOOTDIR/cmdline.txt"
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
install -m 0644 pi/configs/journald-persistent.conf /etc/systemd/journald.conf.d/
install -m 0644 pi/configs/kit-watchdog.conf       /etc/systemd/system.conf.d/99-watchdog.conf
install -m 0644 pi/configs/wifi-powersave-off.conf pi/configs/no-mac-rand.conf /etc/NetworkManager/conf.d/
tar xzf sources/patched-uvc-gadget-sources.tgz -C /home/pi 2>/dev/null || true
chown -R 1000:1000 /home/pi 2>/dev/null || true

# Mask stock Raspberry Pi OS units that have no business on an A/B card.
# rpi-resize / systemd-growfs-root try to GROW THE ROOT PARTITION on first boot - on this
# layout rootA is followed by rootB, so "growing" root would eat the standby slot. They
# failed harmlessly (read-only root) but showed up as failed units; mask them outright.
# cloud-init is not used at all here and just adds three more failures + boot delay.
log "mask stock units that conflict with the A/B layout"
# userconfig.service is the stock "user configuration dialog". It has Restart=on-failure and
# can NEVER succeed on a headless read-only device, so it restart-loops forever: 205 restarts
# in a single boot on the 2026-07-24 test card, which was most of a 17MB journal and constant
# pointless CPU on a Pi whose PSU is already marginal.
for _u in userconfig cloud-config cloud-init-local cloud-init-network cloud-init cloud-init-main \
          cloud-final rpi-resize systemd-growfs-root; do
  systemctl mask "$_u.service" 2>/dev/null || ln -sf /dev/null "/etc/systemd/system/$_u.service"
done

# ---------------- Stage 5b: WiFi setup portal binary (balena wifi-connect) ----------
# bridge-wifi-portal.service needs NetworkManager + dnsmasq-base + this binary. Without
# it the portal loops on "wifi-connect: not found" and the setup AP NEVER appears, so a
# freshly flashed card can't be onboarded at all — found by the 2026-07-23 flash-boot
# test, where the image booted fine but broadcast no BridgeSetup-* network.
# The UI ships in a SEPARATE tarball (the per-arch binary tarball carries none).
log "wifi setup portal (wifi-connect $WC_VER)"
WC_BASE="https://github.com/balena-os/wifi-connect/releases/download/${WC_VER}"
WC_BIN_SHA=413d70e6d1c1366cbe2b32555e8476f3e92878178ed1b9c82205985f055f1936
WC_UI_SHA=e57a3cec559729516decf892beb1e7f191b23e71b2e13bcd43d36b980034ffbe
_wc_tmp="$(mktemp -d)"
curl -fsSL -o "$_wc_tmp/wc.tar.gz" "$WC_BASE/wifi-connect-aarch64-unknown-linux-gnu.tar.gz"
curl -fsSL -o "$_wc_tmp/ui.tar.gz" "$WC_BASE/wifi-connect-ui.tar.gz"
# Pin by hash: this image is signed, so what goes into it is verified too.
echo "$WC_BIN_SHA  $_wc_tmp/wc.tar.gz" | sha256sum -c - || { echo "FATAL: wifi-connect binary hash mismatch"; exit 1; }
echo "$WC_UI_SHA  $_wc_tmp/ui.tar.gz"  | sha256sum -c - || { echo "FATAL: wifi-connect UI hash mismatch"; exit 1; }
tar xzf "$_wc_tmp/wc.tar.gz" -C "$_wc_tmp"
install -D -m 0755 "$_wc_tmp/wifi-connect" /usr/local/sbin/wifi-connect
install -d /usr/local/share/wifi-connect/ui
tar xzf "$_wc_tmp/ui.tar.gz" -C /usr/local/share/wifi-connect/ui
rm -rf "$_wc_tmp"
# Fail loudly here rather than shipping another un-onboardable image.
[ -x /usr/local/sbin/wifi-connect ] || { echo "FATAL: wifi-connect not installed"; exit 1; }
[ -f /usr/local/share/wifi-connect/ui/index.html ] || { echo "FATAL: wifi-connect UI missing"; exit 1; }

# First-boot /data expansion needs these; without them p4 silently stays at its
# factory ~256MB and the rest of the card is wasted (seen on the 2026-07-23 test
# card: /data 272MB with 54.5GB unallocated). Fail the build rather than ship that.
for _t in growpart parted partprobe resize2fs; do
  command -v "$_t" >/dev/null || { echo "FATAL: $_t missing — /data would never expand"; exit 1; }
done

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
         bridge-ab-healthcheck bridge-wifi-unblock bridge-firstdiag \
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
# ---------------- PREFLIGHT: image contains everything its own code calls ----------
log "preflight dependency check"
bash "$REPO/factory/preflight-check.sh"

echo "== ci-build: done (version $IMAGE_VERSION) =="
