#!/bin/bash
# bridge-update.sh — install a whole new NetBridge OS remotely (A/B), with automatic rollback.
#
# For what a signed file update cannot change: packages, systemd units, the loader / installer /
# rollback guard themselves. The running system is never touched:
#
#   fetch manifest + signature -> verify against the OTA key (read-only root) -> fetch the root
#   filesystem (resumable, retried) -> check its sha256 -> write the STANDBY slot -> check the
#   slot really is a bridge root -> arm auto-commit -> trial boot into it. The trial's own
#   health check commits it if the bridge comes up healthy, and reboots back to the old slot if
#   it does not. A power cut during the trial also lands back on the old slot.
#
#   bridge-update.sh --version 2.1.0-abc1234 [--force] [--no-reboot] [--fleet]
#   bridge-update.sh --url https://…/payloads/ota/<version> [...]
#   bridge-update.sh <dir|file://|https://…>              (older form: same as --url)
#
#   --version    fetch from <fleet>/payloads/ota/<version> (CONTROL_URL from the agent config)
#   --force      run even while a presenter session is live / the meeting laptop is attached
#   --no-reboot  stage the slot and stop (inspect it, then: sudo bridge-ab tryboot <slot>)
#   --fleet      for the fleet agent: stage, then trial-boot 45 s later from a separate systemd
#                job, so the command can report "staged" before the bridge reboots
#
# Progress and the last result: /data/ota-staging/status.json (read-file) — the trial boot
# writes "committed" or "rolled back" there too.
#
# Boot partition: kernel + config.txt are SHARED by both slots and not changed by this flow.
# An image that needs a different kernel or config.txt still needs a flash (a warning says so).
set -uo pipefail

PUBKEY="${BRIDGE_OTA_PUBKEY:-/etc/netbridge/ota-pubkey.pem}"
[ -f "$PUBKEY" ] || PUBKEY=/data/config/ota-pubkey.pem
STAGE="${BRIDGE_OTA_STAGE:-/data/ota-staging}"
AGENT_CONF="${BRIDGE_OTA_AGENT_CONF:-/etc/default/bridge-agent}"
BOOT=/boot/firmware
STATUS="$STAGE/status.json"
MIN_FREE_MB=3000

log(){ echo "[ota] $*"; }
status(){ mkdir -p "$STAGE"; printf '{"state":"%s","version":"%s","detail":"%s","ts":%s}\n' \
            "$1" "${VER:-}" "$(printf '%s' "${2:-}" | tr '"\\' "''")" "$(date +%s)" > "$STATUS.tmp" && mv -f "$STATUS.tmp" "$STATUS"; }
die(){ echo "[ota] ERROR: $*" >&2; status failed "$*"; exit "${2:-1}"; }

SRC="" VER_ARG="" FORCE="" NOREBOOT="${OTA_NO_REBOOT:-0}" FLEET="" VER=""
while [ $# -gt 0 ]; do case "$1" in
  --version)   VER_ARG="${2:-}"; shift 2 ;;
  --url)       SRC="${2:-}"; shift 2 ;;
  --force)     FORCE=1; shift ;;
  --no-reboot) NOREBOOT=1; shift ;;
  --fleet)     FLEET=1; shift ;;
  -*)          echo "[ota] unknown option $1" >&2; exit 64 ;;
  *)           SRC="$1"; shift ;;
esac; done
[ -n "$SRC" ] || SRC="${BRIDGE_UPDATE_URL:-}"
if [ -n "$VER_ARG" ]; then
  case "$VER_ARG" in *[!A-Za-z0-9.+-]*) echo "[ota] bad version '$VER_ARG'" >&2; exit 64 ;; esac
  CU="$(sed -n 's/^CONTROL_URL=//p' "$AGENT_CONF" 2>/dev/null | tr -d "\"'" | tail -1)"
  [ -n "$CU" ] || { echo "[ota] no CONTROL_URL in $AGENT_CONF — pass --url" >&2; exit 2; }
  SRC="${CU%/}/payloads/ota/$VER_ARG"
fi

# Tests: show where it would fetch from and stop, before anything needs root.
[ -n "${BRIDGE_OTA_DRYRUN:-}" ] && { echo "source=$SRC force=${FORCE:-0} fleet=${FLEET:-0} noreboot=$NOREBOOT"; exit 0; }
[ "$(id -u)" = 0 ] || die "must run as root" 1
[ -n "$SRC" ] || die "no update source (--version, --url, or BRIDGE_UPDATE_URL)" 2
[ -f "$PUBKEY" ] || die "missing OTA public key" 2
for t in zstd tar openssl curl; do command -v "$t" >/dev/null || die "missing tool: $t" 2; done
[ -x /sbin/mkfs.ext4 ] || command -v mkfs.ext4 >/dev/null || die "missing tool: mkfs.ext4" 2

# An update ends in two reboots. Never start one under a live meeting unless told to.
if [ -z "$FORCE" ]; then
  for f in /sys/class/udc/*/state; do
    [ "$(cat "$f" 2>/dev/null)" = configured ] && die "the meeting laptop is attached — unplug it (or --force)" 8
  done
  pid=$(pgrep -f 'udpsrc port=5000' | head -1)
  if [ -n "$pid" ]; then
    t0=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null); sleep 2
    t1=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null)
    [ "${t1:-0}" -gt "${t0:-0}" ] && die "a presenter session is live — try again after it (or --force)" 8
  fi
fi

mkdir -p "$STAGE"
free_mb=$(df -Pm "$STAGE" | awk 'NR==2 {print $4}')
[ "${free_mb:-0}" -ge "$MIN_FREE_MB" ] || die "only ${free_mb:-?} MB free on /data (need $MIN_FREE_MB)" 2

fetch(){ # fetch <relpath> <dest> — resumable and retried: this runs over venue Wi-Fi
  case "$SRC" in
    http://*|https://*|file://*)
      curl -fSL --retry 8 --retry-delay 5 --retry-all-errors --connect-timeout 20 \
           -C - -o "$2" "$SRC/$1" 2>&1 | tail -2 ;;
    *) cp "$SRC/$1" "$2" ;;
  esac
  [ -s "$2" ]
}

status fetching "manifest from $SRC"
rm -f "$STAGE/manifest.txt" "$STAGE/manifest.txt.sig"
log "fetching manifest + signature from $SRC"
fetch manifest.txt "$STAGE/manifest.txt" || die "could not fetch the manifest" 3
fetch manifest.txt.sig "$STAGE/manifest.txt.sig" || die "could not fetch the manifest signature" 3
log "verifying manifest signature (EC/SHA256) ..."
openssl dgst -sha256 -verify "$PUBKEY" -signature "$STAGE/manifest.txt.sig" "$STAGE/manifest.txt" >/dev/null 2>&1 \
  || die "SIGNATURE VERIFY FAILED — refusing update" 3
log "  signature OK"

VER=$(sed -n 's/^version=//p'  "$STAGE/manifest.txt")
img=$(sed -n 's/^image=//p'    "$STAGE/manifest.txt")
want=$(sed -n 's/^sha256=//p'  "$STAGE/manifest.txt")
kern=$(sed -n 's/^kernel=//p'  "$STAGE/manifest.txt")
[ -n "$VER" ] && [ -n "$img" ] && [ -n "$want" ] || die "manifest incomplete" 3
case "$img" in */*|.*) die "manifest names a bad image file" 3 ;; esac
[ -n "$VER_ARG" ] && [ "$VER" != "$VER_ARG" ] && die "manifest is for $VER, not $VER_ARG" 3
log "  target version=$VER kernel=$kern"

status downloading "$img"
log "downloading $img (resumable) ..."
fetch "$img" "$STAGE/$img" || die "download failed: $img" 4
got=$(sha256sum "$STAGE/$img" | cut -d' ' -f1)
if [ "$got" != "$want" ]; then rm -f "$STAGE/$img"; die "IMAGE HASH MISMATCH (got=$got want=$want) — deleted, retry" 4; fi
log "  image hash OK ($got)"

if [ -n "$kern" ] && [ "$kern" != "$(uname -r)" ]; then
  log "  WARN: image kernel ($kern) != running kernel ($(uname -r)); the shared boot partition is not updated by this flow"
fi

st=$(bridge-ab status | sed -n 's/^standby slot : \([AB]\).*/\1/p'; true)
case "$st" in
  A) dev=/dev/mmcblk0p2; puuid=0d18cc81-02 ;;
  B) dev=/dev/mmcblk0p3; puuid=0d18cc81-03 ;;
  *) die "cannot determine standby slot (got '$st')" 5 ;;
esac
# HARD SAFETY: never touch the running root; confirm PARTUUID
[ "$(findmnt -no SOURCE /)" != "$dev" ] || die "standby dev $dev is the RUNNING root" 6
[ "$(lsblk -no PARTUUID "$dev")" = "$puuid" ] || die "PARTUUID mismatch on $dev" 6
status writing "standby slot $st"
log "writing standby slot $st ($dev, PARTUUID=$puuid)"

mnt=/mnt/ota-standby; mkdir -p "$mnt"; umount "$mnt" 2>/dev/null || true
/sbin/mkfs.ext4 -F -q -L "root$st" "$dev" || die "mkfs failed on $dev" 6
mount "$dev" "$mnt" || die "cannot mount $dev" 6
log "  extracting root filesystem ..."
if ! zstd -d -q --long=31 -c "$STAGE/$img" | tar -xf - -C "$mnt" --numeric-owner --acls --xattrs; then
  umount "$mnt"; die "extraction failed" 6
fi
sync

# normalize standby: its fstab '/' line must use its OWN PARTUUID
awk -v p="$puuid" 'BEGIN{d=0}
  /[ \t]\/[ \t].*ext4/ && !d { sub(/PARTUUID=[0-9a-fA-F-]+/, "PARTUUID=" p); d=1 }
  {print}' "$mnt/etc/fstab" > "$mnt/etc/fstab.$$" && mv "$mnt/etc/fstab.$$" "$mnt/etc/fstab"
# It must really be a bridge root before we boot it: identity binds, read-only overlay, the
# A/B switch, the agent. A payload without them could never pass the trial — only cost a reboot.
bad=""
grep -q "/data/tailscale" "$mnt/etc/fstab" || bad="$bad /data-binds"
[ -f "$mnt/etc/overlayroot.conf" ] || bad="$bad overlayroot"
[ -x "$mnt/usr/local/bin/bridge-ab" ] || bad="$bad bridge-ab"
[ -f "$mnt/usr/local/bin/bridge-agent.py" ] || bad="$bad agent"
echo "$VER" > "$mnt/etc/netbridge-image-version"
umount "$mnt"
/sbin/e2fsck -p -f "$dev" >/dev/null 2>&1 || true
[ -z "$bad" ] || die "the new root is not a complete bridge root (missing:$bad) — not booting it" 6
log "  standby fstab / -> PARTUUID=$puuid; bridge root checks OK"
rm -f "$STAGE/$img"                    # 1.1 GB back to /data; the slot holds it now

: > "$BOOT/.ota-autocommit"
echo "$VER" > "$STAGE/trial-version"
if [ "$NOREBOOT" = 1 ]; then
  status staged "slot $st holds $VER; run: sudo bridge-ab tryboot $st"
  log "STAGED slot $st with version $VER (auto-commit armed). Not rebooting."
  log "  inspect it, then run:  sudo bridge-ab tryboot $st"
  exit 0
fi
if [ -n "$FLEET" ]; then
  systemd-run --unit=bridge-ota-tryboot --collect --on-active=45 /usr/local/bin/bridge-ab tryboot "$st" >/dev/null 2>&1 \
    || die "could not schedule the trial boot" 7
  status staged "slot $st holds $VER; trial boot in 45 s (auto-commit if healthy, auto-rollback if not)"
  log "STAGED $VER in slot $st — trial boot in 45 s; it commits itself if healthy, rolls back if not"
  exit 0
fi
status rebooting "trial boot of slot $st"
log "armed auto-commit; rebooting into a trial of slot $st (auto-commit if healthy, auto-rollback if not)"
exec bridge-ab tryboot "$st"
