#!/bin/bash
# build-disk-image.sh — assemble a flashable, full-disk NetBridge OS image with the
# A/B + persistent-/data layout the fleet actually boots (walkthrough Journey 1:
# "netbridge-os-X.Y.Z.img … flash as many cards as you need"). This is the FACTORY
# counterpart to bridge-update.sh's rootfs.tar.zst OTA: OTA writes ONE standby slot on
# an existing card; THIS writes a whole card from bare.
#
# Runs on a Linux host / CI runner as root (needs loop devices + mkfs). NOT a device
# tool. Consumes a generalized rootfs tarball (from bridge-image-build / the CI image)
# and a boot-partition source, and emits a partitioned .img (optionally xz-compressed).
#
# Layout reproduced EXACTLY from the proven dev card (MBR sig 0x0d18cc81 => PARTUUIDs):
#   p1 BRIDGEBOOT vfat  0d18cc81-01  (bootable)   firmware + kernel612 + overlay initramfs
#   p2 rootA      ext4  0d18cc81-02  committed slot (root= in cmdline.txt)
#   p3 rootB      ext4  0d18cc81-03  standby slot — EMPTY at factory (fills on 1st OTA);
#                                    set POPULATE_ROOTB=1 to clone rootA into it instead
#   p4 bridgedata ext4  0d18cc81-04  LAST partition => grown to fill the card on 1st boot
#
# Read-only overlay root is baked in (overlayroot=tmpfs:recurse=0 + remount-fs masked):
# safe here precisely because /data (p4) exists to hold writes — the reason it could NOT
# ship in the single-partition ci-build-image.sh image.
#
# Usage:
#   build-disk-image.sh --rootfs <rootfs.tar[.zst|.gz]> --boot <bootdir|boot.tar> \
#                       --version <X.Y.Z> --out <out.img> [--pubkey <ota-pubkey.pem>]
# Env knobs (default to full factory sizes; shrink for a structural test build):
#   ROOT_MB=7168   each root slot (>= rootfs + headroom)
#   DATA_MB=256    factory /data (grown on first boot)
#   COMPRESS=0     1 => also emit <out.img>.xz
set -euo pipefail

DISK_ID="0x0d18cc81"           # MBR signature => PARTUUID prefix 0d18cc81
ROOT_MB="${ROOT_MB:-7168}"
DATA_MB="${DATA_MB:-256}"
COMPRESS="${COMPRESS:-0}"
ROOTFS="" BOOTSRC="" VERSION="" OUT="" PUBKEY=""

log(){ echo "[disk] $*"; }
die(){ echo "[disk] ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "must run as root (loop devices + mkfs)"

while [ $# -gt 0 ]; do case "$1" in
  --rootfs)  ROOTFS="$2";  shift 2;;
  --boot)    BOOTSRC="$2"; shift 2;;
  --version) VERSION="$2"; shift 2;;
  --out)     OUT="$2";     shift 2;;
  --pubkey)  PUBKEY="$2";  shift 2;;
  *) die "unknown arg: $1";;
esac; done
[ -n "$ROOTFS" ]  && [ -f "$ROOTFS" ]  || die "--rootfs <tar> required"
[ -n "$BOOTSRC" ] && [ -e "$BOOTSRC" ] || die "--boot <dir|tar> required"
[ -n "$VERSION" ] || die "--version required"
[ -n "$OUT" ]     || die "--out <img> required"

for t in sfdisk losetup mkfs.ext4 mkfs.vfat partprobe; do
  command -v "$t" >/dev/null || die "missing tool: $t"
done

# ---- workspace + cleanup ----------------------------------------------------
LOOP="" ; MNT="$(mktemp -d)" ; ROOTTAR=""
cleanup(){
  set +e
  for m in "$MNT"/p1 "$MNT"/p2 "$MNT"/p3 "$MNT"/p4; do mountpoint -q "$m" && umount "$m"; done
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
  [ -n "$ROOTTAR" ] && rm -f "$ROOTTAR"
  rm -rf "$MNT"
}
trap cleanup EXIT
mkdir -p "$MNT"/p1 "$MNT"/p2 "$MNT"/p3 "$MNT"/p4

# ---- decompress the rootfs tar once (reused for both slots) -----------------
log "staging rootfs tar ($ROOTFS)"
ROOTTAR="$(mktemp --suffix=.tar)"
case "$ROOTFS" in
  *.tar.zst|*.zst) zstd -d -q --long=31 -c "$ROOTFS" > "$ROOTTAR" ;;
  *.tar.gz|*.tgz)  gzip -dc "$ROOTFS" > "$ROOTTAR" ;;
  *.tar)           cp "$ROOTFS" "$ROOTTAR" ;;
  *) die "unrecognized rootfs extension: $ROOTFS" ;;
esac

# ---- size + create the image ------------------------------------------------
BOOT_MB=512
TOTAL_MB=$(( 1 + BOOT_MB + ROOT_MB + ROOT_MB + DATA_MB + 4 ))   # +1 align, +4 slack
log "image ${TOTAL_MB}MB  (boot ${BOOT_MB} / rootA ${ROOT_MB} / rootB ${ROOT_MB} / data ${DATA_MB})"
rm -f "$OUT"
truncate -s "${TOTAL_MB}M" "$OUT"

# ---- partition table (fixed disk id => deterministic PARTUUIDs) -------------
log "partitioning (MBR id $DISK_ID)"
sfdisk --quiet "$OUT" <<EOF
label: dos
label-id: $DISK_ID
unit: sectors
start=2048, size=$((BOOT_MB*2048)), type=c, bootable
size=$((ROOT_MB*2048)), type=83
size=$((ROOT_MB*2048)), type=83
type=83
EOF

# ---- attach + format --------------------------------------------------------
LOOP="$(losetup -Pf --show "$OUT")"
partprobe "$LOOP" 2>/dev/null || true
sleep 1
[ -e "${LOOP}p4" ] || die "loop partitions did not appear (${LOOP}p1..p4)"
log "formatting on $LOOP"
mkfs.vfat -F 32 -n BRIDGEBOOT "${LOOP}p1" >/dev/null
mkfs.ext4 -F -q -L rootA      "${LOOP}p2"
mkfs.ext4 -F -q -L rootB      "${LOOP}p3"
mkfs.ext4 -F -q -L bridgedata "${LOOP}p4"

# verify the PARTUUIDs came out as the fleet expects (fail loud if not)
for n in 1 2 3 4; do
  got="$(blkid -s PARTUUID -o value "${LOOP}p${n}")"
  want="0d18cc81-0${n}"
  [ "$got" = "$want" ] || die "PARTUUID p${n}=$got want $want (disk-id mismatch)"
done
log "PARTUUIDs 0d18cc81-01..04 OK"

# ---- p1: boot partition -----------------------------------------------------
mount "${LOOP}p1" "$MNT/p1"
log "boot: laying down firmware + kernel + overlay initramfs"
if [ -d "$BOOTSRC" ]; then cp -r "$BOOTSRC"/. "$MNT/p1"/
else tar -xf "$BOOTSRC" -C "$MNT/p1"; fi
# committed slot = rootA; read-only overlay on; keep the pinned kernel + overlay initramfs.
cat > "$MNT/p1/cmdline.txt" <<EOF
console=serial0,115200 console=tty1 root=PARTUUID=0d18cc81-02 rootfstype=ext4 fsck.repair=yes rootwait modules-load=dwc2 cfg80211.ieee80211_regdom=IN overlayroot=tmpfs:recurse=0
EOF
# Complete the NetBridge boot config. ci-build-image's stage 4 could NOT do this (the arm-runner
# chroot never mounts the real boot partition), so author it here: pinned kernel, overlay-capable
# initramfs, dwc2 in PERIPHERAL mode (the USB webcam gadget — host mode breaks it), disable-bt,
# arm_freq. Appended under [all] so it wins over any earlier conditional section.
if [ -f "$MNT/p1/config.txt" ] && ! grep -q 'dtoverlay=dwc2,dr_mode=peripheral' "$MNT/p1/config.txt"; then
  cat >> "$MNT/p1/config.txt" <<'CFG'

# --- NetBridge ---
[all]
kernel=kernel612.img
initramfs initramfs612-overlay followkernel
dtoverlay=dwc2,dr_mode=peripheral
dtoverlay=disable-bt
arm_freq=900
CFG
fi
# FAIL LOUD: the pinned kernel + overlay initramfs MUST be on the boot partition, else the card
# boots the wrong kernel and read-only root never engages (the 2026-07-24 flash-test defect).
for _bf in kernel612.img initramfs612-overlay; do
  [ -f "$MNT/p1/$_bf" ] || die "boot partition missing $_bf — pinned kernel / read-only root would fail"
done
log "  boot: kernel612.img + initramfs612-overlay present, config set"
# drop stale committed-slot artifacts a snapshot might carry
rm -f "$MNT/p1/tryboot.txt" "$MNT/p1/cmdline.tryboot" "$MNT/p1/.ota-autocommit" \
      "$MNT/p1"/cmdline.txt.* "$MNT/p1"/config.txt.* 2>/dev/null || true
umount "$MNT/p1"

# ---- helper: populate a root slot -------------------------------------------
populate_root(){ # populate_root <part> <mnt> <root-partuuid> <label>
  local part="$1" mp="$2" ruuid="$3" label="$4"
  mount "$part" "$mp"
  log "$label: extracting rootfs"
  tar -xf "$ROOTTAR" -C "$mp" --numeric-owner --acls --xattrs 2>/dev/null || tar -xf "$ROOTTAR" -C "$mp" --numeric-owner
  # authoritative real fstab (overlayroot rewrites '/' at boot; this is the underlying one)
  cat > "$mp/etc/fstab" <<FSTAB
proc                  /proc           proc    defaults          0       0
PARTUUID=0d18cc81-01  /boot/firmware  vfat    defaults          0       2
PARTUUID=$ruuid  /               ext4    defaults,noatime  0       1
PARTUUID=0d18cc81-04  /data  ext4  defaults,noatime,nofail,x-systemd.device-timeout=10  0  2
/data/tailscale     /var/lib/tailscale    none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
/data/etc-bridge    /etc/bridge           none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
/data/diagnostics   /home/pi/diagnostics  none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
/data/config/nm-connections  /etc/NetworkManager/system-connections  none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
# Runtime-writable scratch on a READ-ONLY root. Without these, services that must write
# outside /data fail hard — dnsmasq could not create /var/lib/misc/dnsmasq.leases, so the
# setup AP came up but served NO DHCP and phones spun forever without an IP (2026-07-24).
# These are all ephemeral by nature, so tmpfs is the right home (and survives power cuts by
# simply not existing). Persistent state still lives on /data.
tmpfs  /var/lib/misc            tmpfs  defaults,noatime,nosuid,nodev,size=8M   0  0
tmpfs  /var/lib/NetworkManager  tmpfs  defaults,noatime,nosuid,nodev,size=8M   0  0
tmpfs  /var/lib/dhcp            tmpfs  defaults,noatime,nosuid,nodev,size=4M   0  0
tmpfs  /var/tmp                 tmpfs  defaults,noatime,nosuid,nodev,size=32M  0  0
# tailscaled uses CacheDirectory=; without a writable /var/cache it dies in a loop with
# "Failed at step CACHE_DIRECTORY ... Read-only file system" (seen 2026-07-24).
tmpfs  /var/cache               tmpfs  defaults,noatime,nosuid,nodev,size=64M  0  0
tmpfs  /var/lib/systemd         tmpfs  defaults,noatime,nosuid,nodev,size=8M   0  0
tmpfs  /var/lib/dhcpcd          tmpfs  defaults,noatime,nosuid,nodev,size=4M   0  0
tmpfs  /var/spool               tmpfs  defaults,noatime,nosuid,nodev,size=8M   0  0
# Persistent logs on /data. Without this journald is volatile on the read-only root and a
# failure is unreadable after power-off - which is what made the portal bugs so hard to find.
/data/log-journal   /var/log/journal   none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
# `bridge set-peer` (and the /api/set-peer endpoint behind "go live") writes the return-audio
# destination to /etc/default/bridge-return-audio. /etc/default is on the READ-ONLY root, so on
# a flashed card that write silently failed and the peer stayed unset - meaning RETURN AUDIO
# COULD NEVER WORK on any read-only image. Bind the single file onto /data. Found 2026-07-24
# during the first end-to-end video test (set-peer returned ok=false, peer stayed "?").
/data/config/bridge-return-audio  /etc/default/bridge-return-audio  none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
# Same read-only trap, swept systematically. bridge-agent is the PROVISIONING file:
# claiming a device writes CONTROL_URL + BOOTSTRAP_TOKEN here, so on a read-only card the
# entire fleet-claim flow failed exactly the way set-peer did - silently.
/data/config/bridge-agent  /etc/default/bridge-agent  none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
/data/config/bridge-net    /etc/default/bridge-net    none  bind,nofail,x-systemd.requires-mounts-for=/data  0  0
FSTAB
  # read-only overlay root (safe: /data holds writes) + remount-fs masked (fails under overlay)
  install -d "$mp/etc"
  # bind target for the return-audio peer file (a file bind needs the file to exist)
  install -d "$mp/etc/default"
  [ -f "$mp/etc/default/bridge-return-audio" ] || \
    printf '# speaker-return destination (the remote peer)\nRETURN_DEST_IP=\nRETURN_DEST_PORT=5004\n' \
      > "$mp/etc/default/bridge-return-audio"
  for _d in bridge-agent bridge-net; do
    [ -f "$mp/etc/default/$_d" ] || printf '# managed on /data (read-only root)\n' > "$mp/etc/default/$_d"
  done
  # the soak log also lived on the read-only root, next to flight.txt
  ln -sf /data/soak.log "$mp/home/pi/soak.log"
  echo 'overlayroot="tmpfs:recurse=0"' > "$mp/etc/overlayroot.conf"
  # DNS on a read-only root. /etc/resolv.conf MUST be a real readable file at boot: dnsmasq
  # reads it when the setup AP starts, and a missing/dangling one makes wifi-connect abort so
  # NO setup AP ever appears (learned the hard way 2026-07-24 - a symlink here broke the AP).
  cat > "$mp/etc/resolv.conf" <<'RESOLV'
nameserver 8.8.8.8
nameserver 1.1.1.1
RESOLV
  chmod 644 "$mp/etc/resolv.conf"
  # ...and stop NetworkManager trying to rewrite it (it logged "could not commit DNS changes
  # ... Read-only file system" on every connect, so the device reported itself offline even
  # though it had associated and taken a DHCP lease).
  install -d -m 755 "$mp/etc/NetworkManager/conf.d"
  printf '[main]\nrc-manager=unmanaged\n' > "$mp/etc/NetworkManager/conf.d/90-rc-manager-unmanaged.conf"
  # flight-recorder writes every second; /home/pi is read-only, so point it at /data.
  install -d "$mp/home/pi"; ln -sf /data/flight.txt "$mp/home/pi/flight.txt"
  ln -sf /dev/null "$mp/etc/systemd/system/systemd-remount-fs.service"
  echo "$VERSION" > "$mp/etc/netbridge-image-version"
  # GENERALIZE (defence in depth; rootfs should already be secret-free)
  rm -f "$mp"/etc/ssh/ssh_host_* 2>/dev/null || true          # per-device, regen on first boot
  # Preserve the fleet config ci-build-image.sh baked in BEFORE removing it from the
  # read-only root. It must be re-seeded into /data/config/bridge-agent (the bind SOURCE)
  # below, or a flashed card boots with an EMPTY /etc/default/bridge-agent and never enrols
  # — the exact defect where a fresh card stayed "raspberrypi" and unclaimed forever.
  mkdir -p "${TMPDIR:-/tmp}/nb-seed"
  cp "$mp/etc/default/bridge-agent" "${TMPDIR:-/tmp}/nb-seed/bridge-agent" 2>/dev/null || true
  rm -f "$mp/etc/default/bridge-agent" 2>/dev/null || true    # provision (below) re-seeds it on /data
  # bind mountpoints must exist + be empty (their content lives on /data)
  install -d "$mp/etc/bridge" "$mp/var/lib/tailscale" \
             "$mp/etc/NetworkManager/system-connections" "$mp/home/pi/diagnostics" \
             "$mp/data" "$mp/var/lib/misc" "$mp/var/lib/NetworkManager" \
             "$mp/var/lib/dhcp" "$mp/var/tmp" "$mp/var/cache" "$mp/var/lib/systemd" \
             "$mp/var/lib/dhcpcd" "$mp/var/spool" "$mp/var/log/journal"
  sync
  umount "$mp"
}
populate_root "${LOOP}p2" "$MNT/p2" "0d18cc81-02" "rootA"
# Factory shrink: rootB is left EMPTY (freshly mkfs'd) so it compresses to ~nothing in the
# published .img.xz — halving the asset (clears GitHub's 2 GiB cap) and letting xz run a
# faster preset. bridge-update.sh mkfs's + fully writes + normalizes the standby on the
# FIRST OTA, so A/B rollback becomes available after the first update. Booting rootB before
# then (nobody should) just fails the health-check and rolls back. Set POPULATE_ROOTB=1 to
# ship both slots populated instead (bigger image, A/B live from flash).
if [ "${POPULATE_ROOTB:-0}" = "1" ]; then
  populate_root "${LOOP}p3" "$MNT/p3" "0d18cc81-03" "rootB"
else
  log "rootB left EMPTY (factory shrink) — first OTA populates it; A/B rollback active after first update"
fi

# ---- p4: /data skeleton (grown to fill the card on first boot) --------------
mount "${LOOP}p4" "$MNT/p4"
log "data: writing /data skeleton"
install -d "$MNT/p4"/log-journal
# Create the /data directory tree FIRST. The config files written just below live under
# config/, and writing them before that directory existed failed the whole build with
# "No such file or directory" (config/ used to be created further down).
install -d "$MNT/p4"/config "$MNT/p4"/config/nm-connections "$MNT/p4"/etc-bridge \
           "$MNT/p4"/diagnostics "$MNT/p4"/tailscale
touch "$MNT/p4"/flight.txt
printf '# speaker-return destination (the remote peer)\nRETURN_DEST_IP=\nRETURN_DEST_PORT=5004\n' \
  > "$MNT/p4"/config/bridge-return-audio
# Seed the /data bind SOURCE for bridge-agent with the fleet config baked at build time
# (captured just before it was stripped from the root). This is what lets a freshly-flashed
# card enrol on its own — J4 step 1. Fall back to the plain comment for an unprovisioned build.
if [ -s "${TMPDIR:-/tmp}/nb-seed/bridge-agent" ]; then
  cp "${TMPDIR:-/tmp}/nb-seed/bridge-agent" "$MNT/p4/config/bridge-agent"
  log "data: seeded bridge-agent config from the baked fleet CONTROL_URL"
else
  printf '# managed on /data (read-only root)\n' > "$MNT/p4/config/bridge-agent"
fi
printf '# managed on /data (read-only root)\n' > "$MNT/p4/config/bridge-net"
touch "$MNT/p4"/soak.log
install -d -o 1000 -g 1000 "$MNT/p4"/fleet-brain 2>/dev/null || install -d "$MNT/p4"/fleet-brain
# secret-free fleet-brain env template (real secrets arrive at claim; DB lives on /data)
cat > "$MNT/p4/config/fleet-brain" <<'ENVF'
DATABASE_URL=sqlite:////data/fleet-brain/bridge.db
ADMIN_API_KEY=
TS_API_KEY=
SMTP_PASSWORD=
BOOTSTRAP_TOKENS=
ENVF
chmod 600 "$MNT/p4/config/fleet-brain"
# OTA root of trust (public key) — required for bridge-update.sh to verify updates
if [ -n "$PUBKEY" ] && [ -f "$PUBKEY" ]; then
  install -m 0644 "$PUBKEY" "$MNT/p4/config/ota-pubkey.pem"; log "  shipped ota-pubkey.pem"
else
  log "  WARN: no --pubkey given; /data/config/ota-pubkey.pem absent (OTA will refuse until present)"
fi
sync
umount "$MNT/p4"

# ---- finish -----------------------------------------------------------------
losetup -d "$LOOP"; LOOP=""
log "done: $OUT ($(numfmt --to=iec $(stat -c%s "$OUT") 2>/dev/null || stat -c%s "$OUT"))"
if [ "$COMPRESS" = "1" ]; then
  log "compressing -> ${OUT}.xz"
  xz -T0 -6 -f "$OUT"
  log "done: ${OUT}.xz ($(numfmt --to=iec $(stat -c%s "${OUT}.xz") 2>/dev/null || stat -c%s "${OUT}.xz"))"
fi
