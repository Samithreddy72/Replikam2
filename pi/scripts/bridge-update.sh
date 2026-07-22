#!/bin/bash
# bridge-update.sh — signed-image A/B OTA (replaces the naive git-pull updater).
# Flow: fetch manifest+sig+image -> verify EC signature over manifest -> verify image
# sha256 -> write the STANDBY slot -> arm auto-commit -> bridge-ab tryboot (standby).
# The trial boot's health-check then auto-commits (healthy) or auto-rolls-back (unhealthy).
#
# Source: BRIDGE_UPDATE_URL (env) or $1 — a local dir, file://, or http(s):// base that
# contains manifest.txt, manifest.txt.sig, and the image named in the manifest.
set -euo pipefail

PUBKEY=/data/config/ota-pubkey.pem
STAGE=/data/ota-staging
BOOT=/boot/firmware
SRC="${BRIDGE_UPDATE_URL:-${1:-}}"

log(){ echo "[ota] $*"; }
die(){ echo "[ota] ERROR: $*" >&2; exit "${2:-1}"; }
[ "$(id -u)" = 0 ] || die "must run as root" 1
[ -n "$SRC" ] || die "no update source (set BRIDGE_UPDATE_URL or pass a URL/dir)" 2
[ -f "$PUBKEY" ] || die "missing pubkey $PUBKEY" 2

fetch(){ # fetch <relpath> <dest>
  case "$SRC" in
    http://*|https://*|file://*) curl -fsSL "$SRC/$1" -o "$2" ;;
    *) cp "$SRC/$1" "$2" ;;
  esac
}

rm -rf "$STAGE"; mkdir -p "$STAGE"
log "fetching manifest + signature from $SRC"
fetch manifest.txt "$STAGE/manifest.txt"
fetch manifest.txt.sig "$STAGE/manifest.txt.sig"

log "verifying manifest signature (EC/SHA256) ..."
openssl dgst -sha256 -verify "$PUBKEY" -signature "$STAGE/manifest.txt.sig" "$STAGE/manifest.txt" >/dev/null 2>&1 \
  || die "SIGNATURE VERIFY FAILED — refusing update" 3
log "  signature OK"

ver=$(sed -n 's/^version=//p'  "$STAGE/manifest.txt")
img=$(sed -n 's/^image=//p'    "$STAGE/manifest.txt")
want=$(sed -n 's/^sha256=//p'  "$STAGE/manifest.txt")
kern=$(sed -n 's/^kernel=//p'  "$STAGE/manifest.txt")
[ -n "$ver" ] && [ -n "$img" ] && [ -n "$want" ] || die "manifest incomplete" 3
log "  target version=$ver kernel=$kern"

log "downloading image $img ..."
fetch "$img" "$STAGE/$img"
got=$(sha256sum "$STAGE/$img" | cut -d' ' -f1)
[ "$got" = "$want" ] || die "IMAGE HASH MISMATCH (got=$got want=$want)" 4
log "  image hash OK ($got)"

# kernel sanity: shared boot partition holds the kernel; warn on mismatch (needs boot update)
installed_kernel=$(uname -r)
if [ -n "$kern" ] && [ "$kern" != "$installed_kernel" ]; then
  log "  WARN: image kernel ($kern) != running kernel ($installed_kernel); shared /boot not updated by this flow"
fi

# resolve standby slot -> device + PARTUUID (trailing `true` keeps set -e happy)
st=$(bridge-ab status | sed -n 's/^standby slot : \([AB]\).*/\1/p'; true)
case "$st" in
  A) dev=/dev/mmcblk0p2; puuid=0d18cc81-02 ;;
  B) dev=/dev/mmcblk0p3; puuid=0d18cc81-03 ;;
  *) die "cannot determine standby slot (got '$st')" 5 ;;
esac
# HARD SAFETY: never touch the running root; confirm PARTUUID
[ "$(findmnt -no SOURCE /)" != "$dev" ] || die "standby dev $dev is the RUNNING root" 6
[ "$(lsblk -no PARTUUID "$dev")" = "$puuid" ] || die "PARTUUID mismatch on $dev" 6
log "writing standby slot $st ($dev, PARTUUID=$puuid)"

mnt=/mnt/ota-standby; mkdir -p "$mnt"; umount "$mnt" 2>/dev/null || true
log "  mkfs.ext4 -F (PARTUUID is a partition-table property, preserved) ..."
/sbin/mkfs.ext4 -F -q -L "root$st" "$dev"
mount "$dev" "$mnt"
log "  extracting rootfs image ..."
zstd -d -q --long=31 -c "$STAGE/$img" | tar -xf - -C "$mnt" --numeric-owner --acls --xattrs
sync

# normalize standby: its fstab '/' line must use its OWN PARTUUID
awk -v p="$puuid" 'BEGIN{d=0}
  /[ \t]\/[ \t].*ext4/ && !d { sub(/PARTUUID=[0-9a-fA-F-]+/, "PARTUUID=" p); d=1 }
  {print}' "$mnt/etc/fstab" > "$mnt/etc/fstab.$$" && mv "$mnt/etc/fstab.$$" "$mnt/etc/fstab"
log "  standby fstab / -> PARTUUID=$puuid"
grep -q "DATABASE_URL=sqlite:////data" "$mnt/etc/default/fleet-brain" 2>/dev/null || log "  WARN: DATABASE_URL missing in image"
grep -q "/data/tailscale" "$mnt/etc/fstab" || log "  WARN: /data binds missing in image fstab"
echo "$ver" > "$mnt/etc/netbridge-image-version"

umount "$mnt"
/sbin/e2fsck -p -f "$dev" >/dev/null 2>&1 || true

# arm hands-off commit for THIS trial, then tryboot the standby slot
: > "$BOOT/.ota-autocommit"
if [ "${OTA_NO_REBOOT:-0}" = "1" ]; then
  log "STAGED slot $st with version $ver (auto-commit armed). Skipping reboot (OTA_NO_REBOOT=1)."
  log "  inspect it, then run:  sudo bridge-ab tryboot $st"
  exit 0
fi
log "armed auto-commit; rebooting into tryboot of slot $st (auto-commit if healthy, auto-rollback if not)"
exec bridge-ab tryboot "$st"
