#!/usr/bin/env bash
# Put a built image into the flash folder, correctly named, only if it passes the audit.
#
# WHY THIS EXISTS
# ---------------
# The folder on the Desktop is the thing that decides what gets flashed at a venue, so the
# rule "1 is the one to flash, 2 is the one to go back to" has to survive being done by hand
# at midnight. Doing the rotation by hand is how an unaudited image ends up in slot 1, or how
# two images end up with names that do not say which came first.
#
# So this does the whole sequence in one command, in a safe order:
#
#   download -> verify signature -> verify sha256 -> unpack -> AUDIT -> only then rotate
#
# If the audit fails, nothing in the folder moves. A failed build cannot become slot 1.
#
#   bash tools/stage-image.sh b2664b5 "why this image exists"
set -euo pipefail

COMMIT="${1:?usage: stage-image.sh <commit> [label]}"
LABEL="${2:-staged}"
DEST="${NETBRIDGE_IMAGE_DIR:-$HOME/Desktop/NetBridge-Image}"
REPO="Samithreddy72/Replikam2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$COMMIT" =~ ^[0-9a-f]{7,40}$ ]] || { echo "commit must be a hexadecimal Git identity"; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "label must use letters, numbers, dots, underscores or hyphens"; exit 2; }
TAG="${NETBRIDGE_IMAGE_TAG:-v$(cat "$ROOT/VERSION")-$COMMIT}"
PUBKEY="${NETBRIDGE_OTA_PUBKEY:-$HOME/.netbridge/keys/ota-pubkey.pem}"
[ -s "$PUBKEY" ] || { echo "pinned owner public key missing: $PUBKEY"; exit 2; }
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[ -d "$DEST" ] || { echo "no image folder at $DEST"; exit 2; }

say "1/5  downloading $TAG"
gh release download "$TAG" -R "$REPO" -D "$WORK" \
  -p "netbridge-os-*.img.xz" -p "manifest-disk.txt*" >/dev/null
XZ=$(ls "$WORK"/netbridge-os-*.img.xz)

say "2/5  verifying the signature and checksum"
openssl dgst -sha256 -verify "$PUBKEY" \
  -signature "$WORK/manifest-disk.txt.sig" "$WORK/manifest-disk.txt"
[ "$(sed -n 's/^version=//p' "$WORK/manifest-disk.txt")" = "${TAG#v}" ] || { echo "manifest version differs from requested release"; exit 1; }
WANT=$(grep '^sha256=' "$WORK/manifest-disk.txt" | cut -d= -f2)
GOT=$(shasum -a 256 "$XZ" | awk '{print $1}')
[ "$WANT" = "$GOT" ] || { echo "CHECKSUM MISMATCH — refusing to stage"; exit 1; }
echo "  sha256 matches the signed manifest"

say "3/5  unpacking"
xz -dc -T0 "$XZ" > "$WORK/img"

say "4/5  auditing (nothing moves unless this passes)"
if ! bash "$(dirname "$0")/image-audit.sh" "$WORK/img" | tee "$WORK/audit.txt"; then
  echo
  echo "AUDIT FAILED — the folder was NOT touched. Slot 1 still holds the previous image."
  exit 1
fi

# Build time in local wall-clock, because the point of the timestamp is that a human reading
# the folder at a venue can tell which image is newer without converting anything.
PUB=$(gh release view "$TAG" -R "$REPO" --json publishedAt,createdAt -q '.publishedAt // .createdAt')
STAMP=$(python3 -c "
import datetime,zoneinfo,sys
t=datetime.datetime.strptime(sys.argv[1],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
print(t.astimezone(zoneinfo.ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d_%H%M'))" "$PUB")

say "5/5  rotating the folder"
mkdir -p "$DEST/Archive"
# Two working images stay visible at all times. One rollback is not enough: if slot 2 shares
# whatever is wrong with slot 1, there has to be somewhere else to go without digging through
# Archive at a venue. So the chain is 1 -> 2 -> 3 -> Archive.
for f in "$DEST"/3--ROLLBACK-OLDER--*.img.xz; do
  [ -e "$f" ] || continue
  b=$(basename "$f"); mv -f "$f" "$DEST/Archive/${b#3--ROLLBACK-OLDER--}"
  echo "  archived  ${b#3--ROLLBACK-OLDER--}"
done
for f in "$DEST"/2--ROLLBACK--*.img.xz; do
  [ -e "$f" ] || continue
  b=$(basename "$f"); mv -f "$f" "$DEST/3--ROLLBACK-OLDER--${b#2--ROLLBACK--}"
  echo "  older     ${b#2--ROLLBACK--}"
done
mkdir -p "$DEST/older-good-manifest"
cp -f "$DEST/known-good-manifest/." "$DEST/older-good-manifest/" 2>/dev/null || \
  cp -f "$DEST"/known-good-manifest/* "$DEST/older-good-manifest/" 2>/dev/null || true
# old 1 -> slot 2, and its manifest becomes the known-good manifest
for f in "$DEST"/1--FLASH-THIS--*.img.xz; do
  [ -e "$f" ] || continue
  b=$(basename "$f"); mv -f "$f" "$DEST/2--ROLLBACK--${b#1--FLASH-THIS--}"
  echo "  rollback  ${b#1--FLASH-THIS--}"
done
mkdir -p "$DEST/known-good-manifest"
for m in manifest-disk.txt manifest-disk.txt.sig; do
  [ -f "$DEST/1--${m/manifest-disk/manifest}" ] &&
    cp -f "$DEST/1--${m/manifest-disk/manifest}" "$DEST/known-good-manifest/$m"
done || true
# new image -> slot 1
NEW="1--FLASH-THIS--${STAMP}-IST--${COMMIT}--${LABEL}.img.xz"
mv -f "$XZ" "$DEST/$NEW"
cp -f "$WORK/manifest-disk.txt"     "$DEST/1--manifest.txt"
cp -f "$WORK/manifest-disk.txt.sig" "$DEST/1--manifest.txt.sig"
cp -f "$WORK/manifest-disk.txt"     "$DEST/manifest-disk.txt"
cp -f "$WORK/manifest-disk.txt.sig" "$DEST/manifest-disk.txt.sig"
cp -f "$PUBKEY"                     "$DEST/ota-pubkey.pem"
sed 's/\x1b\[[0-9;]*m//g' "$WORK/audit.txt" > "$DEST/1--AUDIT.txt"

echo
echo "  flash this:  $NEW"
echo "  roll back:   $(basename "$(ls "$DEST"/2--ROLLBACK--*.img.xz 2>/dev/null | head -1)")"
echo "  two steps:   $(basename "$(ls "$DEST"/3--ROLLBACK-OLDER--*.img.xz 2>/dev/null | head -1)")"
echo
echo "  ROLLBACK.txt and WHATS-IN-THIS-IMAGE.txt are written by hand — update them to match."
