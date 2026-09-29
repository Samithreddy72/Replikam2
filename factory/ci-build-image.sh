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
  python3 python3-pil python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 git meson ninja-build build-essential \
  gcc-12 cpp-12 gcc-12-base libgcc-12-dev \
  network-manager dnsmasq-base rsync ca-certificates curl \
  cloud-guest-utils parted e2fsprogs \
  rfkill iw \
  fonts-dejavu-core \
  nftables
# nftables: the PIN's media gate (bridge-pin) - video/voice ports admit only the presenter who
# entered the PIN. Only the `nft` tool is used; Debian's own nftables.service stays disabled
# below, because its /etc/nftables.conf starts with "flush ruleset" (it would wipe the gate).
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
dkms autoinstall -k "$KVER" || { echo "FATAL: kernel module build failed"; exit 1; }
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

# --- power: cut the current SPIKES, not just the average ---
# The 4.63V trip is caused by transients, not steady draw. A field bridge logged 22
# "Undervoltage detected!" events in one boot, clustered at 13.7s / 21.8s / 31.9s — exactly
# when all cores ramp, the USB gadget enumerates and the Wi-Fi radio comes up. Runtime
# trimming (bridge-powertrim: HDMI, LEDs, eth0, USB-A host, freq cap) only starts AFTER
# those spikes have happened, so the boot dips have to be handled here.
#
# arm_freq=900 above caps the ceiling; arm_boost=0 stops the Pi 4 requesting the 1.8GHz
# turbo rail at all, which is where the worst transient lives.
arm_boost=0
# Headless: no display pipeline and no analog audio (the UAC2 gadget is the only audio
# path).
# Minimum gpu_mem also hands ~112MB of RAM back to the system. (64 was tried on 2026-09-22 to
# enable the H.264 hardware decoder; that path measured worse live, so the feeder decodes in
# software and the codec block is not needed.)
gpu_mem=16
dtparam=audio=off
# LEDs dark from boot rather than from whenever powertrim runs — a few mA, but free, and it
# covers the one window powertrim cannot reach.
dtparam=act_led_trigger=none
dtparam=act_led_activelow=off
dtparam=pwr_led_trigger=none
dtparam=pwr_led_activelow=off
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
install -d -m 0755 /etc/NetworkManager/dispatcher.d
install -m 0755 pi/scripts/bridge-fleet-network-up /etc/NetworkManager/dispatcher.d/90-netbridge-fleet
bash tools/build-uvc-source.sh /tmp/netbridge-uvc-build || exit 1
install -d /etc/netbridge
printf "1\n" > /etc/netbridge/video-fallback-v1
install -d /etc/netbridge
printf 'required\n' > /etc/netbridge/script-signature-v2
install -d /etc/modprobe.d /etc/systemd/journald.conf.d /etc/systemd/system.conf.d /etc/NetworkManager/conf.d
# The script-override TRUST ANCHOR, on the read-only root. Without it every bridge refuses
# signed remote deploys ("no pubkey ... cannot verify") and card surgery comes back. It must
# NOT live on /data: that partition is writable and holds the overrides themselves, so a key
# there could be swapped by whoever can write it.
install -d /etc/netbridge
install -m 0644 pi/configs/script-pubkey.pem /etc/netbridge/script-pubkey.pem
# The same idea for the rest of remote maintenance (2026-09-24), all on the read-only root so
# nothing a remote update can write is able to widen what remote updates may do:
#   ota-pubkey.pem             verifies whole-OS updates (bridge-update.sh); /data had the only
#                              copy before, and /data is writable
#   updatable.conf             the ONLY files a signed update may replace, and how each applies
#   owner_ssh_authorized_keys  the owner's SSH key (bridge-ssh.sh: tailnet only, no root, no
#                              passwords); rotated by a signed update, never by editing
install -m 0644 pi/configs/ota-pubkey.pem /etc/netbridge/ota-pubkey.pem
install -m 0644 pi/configs/updatable.conf /etc/netbridge/updatable.conf
install -m 0644 pi/configs/owner_ssh_authorized_keys /etc/netbridge/owner_ssh_authorized_keys
install -m 0644 pi/configs/v4l2loopback.conf       /etc/modprobe.d/
install -m 0644 pi/configs/size-cap.conf           /etc/systemd/journald.conf.d/
install -m 0644 pi/configs/journald-persistent.conf /etc/systemd/journald.conf.d/
install -m 0644 pi/configs/no-kmsg.conf              /etc/systemd/journald.conf.d/
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

# bridge-web.service runs as User=pi, and /dev/vcio is root:video 0660. The old manual
# setup.sh inherited the stock pi group membership; this CI image never reproduced it, so
# every vcgencmd call from bridge-web failed with "Can't open device file: /dev/vcio_gencmd"
# and the fleet was BLIND to temperature and undervoltage on flashed cards - exactly the
# telemetry that matters on a Pi with a marginal PSU. Found 2026-07-24.
log "hardware group membership for the pi user"
for _g in video gpio i2c spi input render; do
  getent group "$_g" >/dev/null 2>&1 && usermod -aG "$_g" pi 2>/dev/null || true
done
# Owner key login requires a real shell; retain a locked password.
usermod -s /bin/bash pi && usermod -p '!' pi || { echo 'FATAL: cannot configure owner login'; exit 1; }
[ "$(getent passwd pi | cut -d: -f7)" = /bin/bash ] || { echo 'FATAL: owner login shell unavailable'; exit 1; }
id pi 2>/dev/null || true

# Passwordless sudo for pi. Raspberry Pi OS ships /etc/sudoers.d/010_pi-nopasswd; this CI
# image did not, so every privileged action bridge-web performs as User=pi died on `sudo -n`
# - including /api/set-peer, i.e. the app's GO LIVE button, and bridge-pin. The failure was
# invisible: the endpoint returned ok=false with changed=true and the UI read it as success.
log "passwordless sudo for pi (raspios default; bridge-web depends on it)"
printf 'pi ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/010_pi-nopasswd
chmod 0440 /etc/sudoers.d/010_pi-nopasswd
visudo -c -f /etc/sudoers.d/010_pi-nopasswd || { echo "FATAL: bad sudoers file"; exit 1; }

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
         gadget-clean-detach bridge-agent.timer \
         bridge-overrides bridge-overrides-health.timer bridge-ssh \
         bridge-ab-healthcheck bridge-wifi-unblock bridge-firstdiag \
         bridge-firstboot bridge-regen-hostkeys bridge-identity \
         bridge-pin-gate bridge-pin-sessions; do
  systemctl enable "$u" 2>/dev/null || echo "WARN: could not enable $u"
done
systemctl disable bridge-testpattern 2>/dev/null || true
# The distro firewall loader must never run: its config flushes every rule, the PIN gate included.
systemctl disable nftables.service 2>/dev/null || true
# The stock sshd stays OFF: it would listen on every interface with the distro config. The
# bridge's SSH is bridge-ssh.service — tailnet address only, owner key only.
systemctl disable ssh.service ssh.socket 2>/dev/null || true
# Installed but deliberately NOT enabled, and both for the same reason: neither has ever been
# shown to help, and every service running during a live session is another variable in an
# audio fault we have not yet explained.
#
#   bridge-crackle-sentry  built 21 Jul, enabled 13 Aug, never once caught a real crackle.
#                          While a session holds the PCM it only greps the journal, so it is
#                          cheap - but "cheap and unproven" is still not a reason to run it.
#   bridge-pitch           does nothing at all unless the gadget is in c_sync=async, and the
#                          image ships adaptive. Enabling a daemon that exits immediately
#                          buys nothing and hides the fact that the feature is untested on
#                          hardware. Enable it deliberately when running that experiment.
systemctl disable bridge-crackle-sentry 2>/dev/null || true
systemctl disable bridge-pitch 2>/dev/null || true

# ---------------- GENERALIZE: strip ALL per-device identity (secret-free) ----------------
log "generalize -> secret-free"
rm -f  /etc/bridge/agent.token /etc/bridge/setup-wifi-pass /etc/bridge/version
rm -f  /etc/bridge/bridge-provision.conf /boot/firmware/bridge-provision.conf
rm -rf /var/lib/tailscale/*                       # no tailnet identity on the card
# Fleet enrolment config. The walkthrough makes two promises that only hold together if
# this is baked in at BUILD time: J1 "all identical, no per-device config file, no keys to
# inject" and J4 "the moment a shipped bridge gets internet, it shows up as unclaimed".
# CONTROL_URL and the bootstrap token are FLEET-wide, not per-device, so writing them here
# keeps J1 true while making J4 possible. Previously this file was deleted outright, so a
# shipped card could never enrol on its own and J4 step 1 simply did not happen.
# Passed in by the workflow: FLEET_CONTROL_URL / FLEET_BOOTSTRAP_TOKEN.
if [ -n "${FLEET_CONTROL_URL:-}" ]; then
  log "baking fleet CONTROL_URL into the image ($FLEET_CONTROL_URL)"
  {
    printf 'CONTROL_URL=%s\n' "$FLEET_CONTROL_URL"
    [ -n "${FLEET_BOOTSTRAP_TOKEN:-}" ] && printf 'BOOTSTRAP_TOKEN=%s\n' "$FLEET_BOOTSTRAP_TOKEN"
  } > /etc/default/bridge-agent
  # 0600, NOT 0644.
  #
  # This file carries the FLEET-WIDE bootstrap token. At 0644 any local account on the bridge
  # could read it, and anyone who obtained a card could extract it and enrol rogue devices
  # against the fleet. bridge-agent runs as root (no User= in its unit), so nothing legitimate
  # needs group or world access; bridge-web runs as `pi` and does not read this file at all.
  #
  # It was also inconsistent: bridge-firstboot.sh writes exactly this file with `umask 077`
  # and `chmod 600`, but that path only runs when a provision conf is present -- and the
  # documented normal case is "all identical, no per-device config file", so the 0644 baked
  # here is what actually shipped. Two writers, two permissions, and the weaker one won.
  chmod 0600 /etc/default/bridge-agent
else
  # No fleet configured for this build: ship an EMPTY file, not a missing one. The agent
  # exits 0 when unprovisioned, and /etc/default/bridge-agent is a /data bind target -
  # a file bind fails to mount if the target does not exist.
  printf '# no fleet configured at build time; set CONTROL_URL to enrol\n' > /etc/default/bridge-agent
fi
rm -f  /etc/NetworkManager/system-connections/*   # no Wi-Fi PSKs baked in (onboard via setup portal)
rm -f  /etc/ssh/ssh_host_*                         # regenerated per-device by bridge-regen-hostkeys on first boot
# fleet-brain is NOT part of the bridge image (it is the separate control plane) — nothing to strip here.
install -d /etc/bridge
echo "$IMAGE_VERSION" > /etc/bridge/version        # bridge-firstboot re-stamps with the flashed BRIDGE_VERSION
# STRUCTURED PROVENANCE, not just a version string.
#
# "2.0.0-1db20ec" carries a short SHA, and that suffix is the only part of it that means
# anything: the "2.0.0" is a manually typed CI input, which is how a JULY image ended up
# labelled 2.0.1 while AUGUST images say 2.0.0. Answering "what exactly is this device
# running?" should not require a human to recognise a 7-character prefix.
#
# The image cannot contain its own sha256 (writing it in would change it), so the digest lives
# in the signed manifest beside the artifact. Everything the image CAN know about itself is
# recorded here, and bridge-web serves it at /api/status as `build`.
cat > /etc/bridge/release.json <<RELEOF
{
  "version": "${IMAGE_VERSION}",
  "git_sha": "${BUILD_GIT_SHA:-unknown}",
  "git_short": "${IMAGE_VERSION##*-}",
  "build_id": "${BUILD_ID:-local}",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "builder": "${BUILD_URL:-local}"
}
RELEOF
chmod 0644 /etc/bridge/release.json
# ...and where it can be READ. /etc/bridge is a bind mount from /data at runtime, so the copy
# above is hidden on every running bridge (and /data is shared by both A/B slots). The slot's own
# root carries this one; bridge-web reads it first (2026-09-28).
cp /etc/bridge/release.json /etc/netbridge-release.json
chmod 0644 /etc/netbridge-release.json

# ---------------- Verify no obvious secrets survived ----------------
log "secret sweep"
# Capture matches into a variable, NOT `grep | while read` — a pipeline runs the loop in a
# subshell, so a LEAK=1 set inside it was lost and the build shipped secrets anyway (the old
# no-op gate). Read the results here, then FAIL LOUD if anything survived generalization.
LEAKS="$(grep -rIlE 'tskey-|BEGIN OPENSSH PRIVATE KEY|psk=.+|ADMIN_API_KEY=.+' \
   /etc/NetworkManager /etc/default /var/lib/tailscale /etc/ssh /etc/bridge 2>/dev/null || true)"
if [ -n "$LEAKS" ]; then
   echo "  SECRET SWEEP FAILED — per-device secrets survived generalization:" >&2
   while IFS= read -r f; do echo "  LEAK: $f" >&2; done <<< "$LEAKS"
   exit 1
fi
# The sweep above hunts secrets that should not be here AT ALL. The bootstrap token is
# different: it is deliberately baked in (fleet-wide, and J4 "a shipped bridge shows up as
# unclaimed" depends on it). But a deliberate secret still has to be protected, and a sweep
# that stays silent about it cannot tell "intentionally present and locked down" from
# "intentionally present and world-readable" -- which is exactly how it shipped at 0644.
if [ -s /etc/default/bridge-agent ] && grep -q '^BOOTSTRAP_TOKEN=.' /etc/default/bridge-agent; then
  mode="$(stat -c '%a' /etc/default/bridge-agent 2>/dev/null || echo '?')"
  if [ "$mode" != "600" ]; then
    echo "  SECRET SWEEP FAILED — /etc/default/bridge-agent holds the fleet bootstrap token" >&2
    echo "  but is mode $mode (expected 600). Any local account could read it." >&2
    exit 1
  fi
  log "bootstrap token present and correctly restricted (0600)"
fi
log "secret sweep clean"
# ---------------- PREFLIGHT: image contains everything its own code calls ----------
log "preflight dependency check"
bash "$REPO/factory/preflight-check.sh" || { echo "FATAL: image preflight failed"; exit 1; }

# ---------------- Strip the build-time repository copy ----------------
# The whole checkout is copied to /opt/replikam2 so this script can install from it. Every
# bridge script is `install`ed into /usr/local/bin above, and NOTHING on the device references
# /opt/replikam2 at runtime -- so once the install is done the copy is dead weight.
#
# It is not small dead weight: the .git directory alone is ~50 MB of commit history, shipped
# into every meeting room this product is plugged into, on a card the release notes already
# ask people to treat as confidential. Leaving the full source and history on a device that
# goes to other organisations' sites buys nothing and costs both space and disclosure.
#
# Removed LAST, after preflight-check.sh -- which executes out of $REPO. The first version of
# this stripped the copy before the sweep and would have deleted the tree the very next step
# runs from; caught by reading what still referenced $REPO below the insertion point, before
# the build was started rather than after it failed.
if [ -d "$REPO" ]; then
  log "removing the build-time repo copy from the image ($(du -sh "$REPO" 2>/dev/null | cut -f1))"
  rm -rf "$REPO"
fi

echo "== ci-build: done (version $IMAGE_VERSION) =="
