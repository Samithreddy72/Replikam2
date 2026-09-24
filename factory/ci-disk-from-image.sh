#!/bin/bash
# ci-disk-from-image.sh — the CI glue that turns the arm-runner's single-partition
# raspios image into the two artifacts the fleet actually consumes, then signs them:
#
#   1. rootfs.tar.zst           — the OTA payload for bridge-update.sh (writes a STANDBY slot);
#                                  since 2026-09-24 it is rootA exactly as flashed (step 3b)
#   2. netbridge-os-<V>.img.xz  — the flashable full-disk A/B + /data image (whole-card provision)
#
# Kept as a script (not inline YAML) so it is testable off-CI: point --image at any
# raspios-style .img (p1 FAT boot, p2 ext4 root) and it produces + signs everything.
# Runs on a Linux host / CI runner as root (loop devices + mkfs).
#
# Usage:
#   ci-disk-from-image.sh --image <arm-runner.img> --version <V> --outdir <dir> \
#                        [--signing-key <ota-signing-key.pem>] [--root-mb N] [--data-mb N]
# Without --signing-key it still builds everything, just UNSIGNED (no .sig, no pubkey).
set -euo pipefail
KVER="6.12.93+rpt-rpi-v8"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="" VERSION="" OUTDIR="" KEY="" ROOT_MB="${ROOT_MB:-4096}" DATA_MB="${DATA_MB:-256}"

log(){ echo "[ci-disk] $*"; }
die(){ echo "[ci-disk] ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "must run as root"
while [ $# -gt 0 ]; do case "$1" in
  --image)       IMAGE="$2";   shift 2;;
  --version)     VERSION="$2"; shift 2;;
  --outdir)      OUTDIR="$2";  shift 2;;
  --signing-key) KEY="$2";     shift 2;;
  --root-mb)     ROOT_MB="$2"; shift 2;;
  --data-mb)     DATA_MB="$2"; shift 2;;
  *) die "unknown arg: $1";;
esac; done
[ -n "$IMAGE" ]   && [ -f "$IMAGE" ] || die "--image <raspios.img> required"
[ -n "$VERSION" ] || die "--version required"
[ -n "$OUTDIR" ]  || die "--outdir required"
[ -f "$HERE/build-disk-image.sh" ] || die "build-disk-image.sh not next to this script"
for t in losetup sfdisk mkfs.vfat mkfs.ext4 zstd openssl xz; do command -v "$t" >/dev/null || die "missing tool: $t"; done

mkdir -p "$OUTDIR"; OUTDIR="$(cd "$OUTDIR" && pwd)"
WORK="$(mktemp -d)"; LOOP=""; OTA_LOOP=""
cleanup(){ set +e
  for m in "$WORK"/root "$WORK"/boot "$WORK"/rootA; do mountpoint -q "$m" && umount "$m"; done
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
  [ -n "$OTA_LOOP" ] && losetup -d "$OTA_LOOP" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/root" "$WORK/boot" "$WORK/bootsrc"

# ---- 1. extract rootfs (p2) + boot (p1) from the raspios image --------------
log "extracting rootfs + boot from $IMAGE"
LOOP="$(losetup -Pf --show "$IMAGE")"; partprobe "$LOOP" 2>/dev/null || true; sleep 1
[ -e "${LOOP}p2" ] || die "expected 2-partition raspios image (p1 boot, p2 root)"
mount "${LOOP}p2" "$WORK/root"
mount "${LOOP}p1" "$WORK/boot"
tar --numeric-owner --acls --xattrs --warning=no-file-changed \
    --exclude='./proc/*' --exclude='./sys/*' --exclude='./dev/*' --exclude='./run/*' \
    --exclude='./tmp/*' --exclude='./lost+found' \
    -C "$WORK/root" -cf "$WORK/rootfs.tar" . || [ $? -le 1 ]
cp -r "$WORK/boot"/. "$WORK/bootsrc"/
# ci-build-image writes the pinned kernel + overlay initramfs to /boot/firmware INSIDE the
# rootfs (the arm-runner chroot never mounts the real boot partition), so they land on the
# ROOT fs, not the boot partition. Lift them onto the boot-partition contents here — without
# this the card boots the default kernel and read-only root never engages.
for _f in kernel612.img initramfs612 initramfs612-overlay; do
  if [ -f "$WORK/bootsrc/$_f" ]; then log "  $_f already on boot partition"; continue; fi
  _src="$(find "$WORK/root" -maxdepth 4 -name "$_f" 2>/dev/null | head -1)"
  if [ -n "$_src" ]; then cp -f "$_src" "$WORK/bootsrc/$_f"; log "  lifted $_f onto boot (from ${_src#$WORK/root})"
  else log "  WARN: $_f found on NEITHER boot partition NOR rootfs"; fi
done
umount "$WORK/root" "$WORK/boot"
losetup -d "$LOOP"; LOOP=""
log "compressing rootfs.tar.zst (zstd -12 --long)"
zstd -q -12 --long=27 -T0 -f "$WORK/rootfs.tar" -o "$OUTDIR/rootfs.tar.zst"
rm -f "$WORK/rootfs.tar"

# ---- 2. OTA root-of-trust pubkey, derived from the signing key --------------
PUBKEY_ARG=""
if [ -n "$KEY" ] && [ -f "$KEY" ]; then
  log "deriving ota-pubkey.pem from signing key"
  openssl pkey -in "$KEY" -pubout -out "$OUTDIR/ota-pubkey.pem" 2>/dev/null \
    || openssl ec -in "$KEY" -pubout -out "$OUTDIR/ota-pubkey.pem"
  PUBKEY_ARG="--pubkey $OUTDIR/ota-pubkey.pem"
  # The image's read-only root carries pi/configs/ota-pubkey.pem as the OTA trust anchor. It
  # must be the public half of THIS signing key, or every bridge built from here would refuse
  # every future update.
  _k1="$(openssl pkey -pubin -in "$OUTDIR/ota-pubkey.pem" -outform DER 2>/dev/null | sha256sum | cut -c1-64)"
  _k2="$(openssl pkey -pubin -in "$HERE/../pi/configs/ota-pubkey.pem" -outform DER 2>/dev/null | sha256sum | cut -c1-64)"
  [ -n "$_k1" ] && [ "$_k1" = "$_k2" ] || die "pi/configs/ota-pubkey.pem is not the public key of the OTA signing key"
else
  log "WARN: no --signing-key; building UNSIGNED (no .sig / no shipped pubkey)"
fi

# ---- 3. assemble the flashable full-disk image ------------------------------
DISK="$WORK/netbridge-os-${VERSION}.img"
log "assembling full-disk image (ROOT_MB=$ROOT_MB DATA_MB=$DATA_MB)"
ROOT_MB="$ROOT_MB" DATA_MB="$DATA_MB" bash "$HERE/build-disk-image.sh" \
  --rootfs "$OUTDIR/rootfs.tar.zst" --boot "$WORK/bootsrc" \
  --version "$VERSION" --out "$DISK" $PUBKEY_ARG
# ---- 3b. the OTA payload = rootA exactly as flashed ---------------------------
# rootfs.tar.zst above is the RAW Raspberry Pi OS root. build-disk-image.sh turns it into the
# bridge's rootA: read-only overlay config, the /data binds for tailscale / NetworkManager /
# journal / /etc/bridge / agent + media config, resolv.conf, the flight-recorder link. An OTA
# must install THAT — the raw root boots without the bridge's identity (no tailnet state, no
# Wi-Fi, no fleet token), so every trial would fail its health check and roll back. Found
# 2026-09-24 by reading both scripts side by side; no OTA had ever been run end to end.
log "OTA payload: re-packing rootA from the assembled disk image"
OTA_LOOP="$(losetup -fP --show "$DISK")"
mkdir -p "$WORK/rootA"
mount -o ro "${OTA_LOOP}p2" "$WORK/rootA"
for _must in etc/overlayroot.conf etc/fstab usr/local/bin/bridge-overrides.sh etc/netbridge/updatable.conf; do
  [ -e "$WORK/rootA/$_must" ] || die "flashed rootA lacks $_must — refusing to publish an OTA payload"
done
grep -q "/data/tailscale" "$WORK/rootA/etc/fstab" || die "flashed rootA fstab lacks the /data binds"
tar --numeric-owner --acls --xattrs --warning=no-file-changed \
    -C "$WORK/rootA" -cf "$WORK/rootfs-ota.tar" . || [ $? -le 1 ]
umount "$WORK/rootA"; losetup -d "$OTA_LOOP"; OTA_LOOP=""
zstd -q -12 --long=27 -T0 -f "$WORK/rootfs-ota.tar" -o "$OUTDIR/rootfs.tar.zst"
rm -f "$WORK/rootfs-ota.tar"
log "  OTA rootfs.tar.zst = the flashed rootA ($(du -h "$OUTDIR/rootfs.tar.zst" | cut -f1))"

log "compressing disk image -> .img.xz (xz -${XZ_LEVEL:-3} -T0)"
xz -T0 "-${XZ_LEVEL:-3}" -f "$DISK"   # rootB is empty (factory shrink) so a fast preset
mv "${DISK}.xz" "$OUTDIR/"            # still lands well under GitHub's 2 GiB asset cap

# ---- 4. manifests (OTA + disk) + signatures ---------------------------------
BUILT="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
write_manifest(){ # write_manifest <file> <product> <image-name>
  local mf="$1" product="$2" img="$3"
  local sha size
  sha="$(sha256sum "$OUTDIR/$img" | cut -d' ' -f1)"
  size="$(stat -c%s "$OUTDIR/$img")"
  cat > "$OUTDIR/$mf" <<EOF
product=$product
version=$VERSION
image=$img
sha256=$sha
size=$size
kernel=$KVER
built=$BUILT
EOF
  if [ -n "$KEY" ] && [ -f "$KEY" ]; then
    openssl dgst -sha256 -sign "$KEY" -out "$OUTDIR/${mf}.sig" "$OUTDIR/$mf"
    log "signed $mf"
  fi
}
# manifest.txt is the OTA manifest bridge-update.sh consumes (image = the rootfs tarball)
write_manifest manifest.txt      netbridge-os      rootfs.tar.zst
write_manifest manifest-disk.txt netbridge-os-disk "netbridge-os-${VERSION}.img.xz"

log "done. artifacts in $OUTDIR:"
ls -1 "$OUTDIR" | sed 's/^/  /'
