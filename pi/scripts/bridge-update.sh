#!/bin/bash
# bridge-update.sh — install a whole new NetBridge OS remotely (A/B), with automatic rollback.
#
# For what a signed file update cannot change: packages, systemd units, the loader / installer /
# rollback guard themselves. The running system is never touched:
#
#   fetch manifest + signature -> verify against the OTA key (read-only root) -> fetch the root
#   filesystem (resumable, retried, within the job's time limit) -> wait until no meeting is in
#   progress -> check its sha256 -> write the STANDBY slot -> check the slot really is a bridge
#   root -> arm auto-commit -> trial boot into it once no meeting is in progress. The trial's own
#   health check commits it if the bridge comes up healthy, and reboots back to the old slot if
#   it does not. A power cut during the trial also lands back on the old slot, and the next boot
#   reports it (bridge-ab healthcheck).
#
#   bridge-update.sh --version 2.1.0-abc1234 [--force] [--no-reboot] [--fleet]
#   bridge-update.sh --url https://…/payloads/ota/<version> [...]
#   bridge-update.sh <dir|file://|https://…>              (older form: same as --url)
#   bridge-update.sh --tryboot-when-idle <A|B> [--force]   (scheduled by --fleet, see below)
#
#   --version    fetch from <fleet>/payloads/ota/<version> (CONTROL_URL from the agent config)
#   --force      run even while a presenter session is live / the meeting laptop is attached
#   --no-reboot  stage the slot and stop (inspect it, then: sudo bridge-ab tryboot <slot>)
#   --fleet      for the fleet agent: stage, then trial-boot from a separate systemd job 45 s
#                later - or once the meeting ends, if one is in progress by then - so the
#                command can report "staged" before the bridge reboots
#
# Exit status: 0 staged (or rebooting into the trial) · 2 a prerequisite is missing · 3 manifest
# · 4 download · 5/6 standby slot · 7 the trial boot could not be scheduled · 8 a meeting is in
# progress (nothing was written; a finished download is kept) · 9 another OS update is already
# running · 10 stopped part-way (time limit or shutdown).
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
BOOT="${BRIDGE_OTA_BOOT:-/boot/firmware}"
STATUS="$STAGE/status.json"
MIN_FREE_MB=3000
# The bridge's paths and tools; a test sandbox replaces them.
MNT="${BRIDGE_OTA_MNT:-/run/ota-standby}"
MKFS="${BRIDGE_OTA_MKFS:-/sbin/mkfs.ext4}"
FSCK="${BRIDGE_OTA_FSCK:-/sbin/e2fsck}"
PROC_CMDLINE="${BRIDGE_OTA_PROC_CMDLINE:-/proc/cmdline}"
BOOT_ID="${BRIDGE_OTA_BOOT_ID:-/proc/sys/kernel/random/boot_id}"
UDC_GLOB="${BRIDGE_OTA_UDC_GLOB:-/sys/class/udc/*/state}"
LIVE_CMD="${BRIDGE_OTA_LIVE_CMD:-}"        # tests: a command whose success means "a presenter is live"
LOCK="${BRIDGE_OTA_LOCK:-/run/bridge-update.lock}"
SYSTEMCTL="${BRIDGE_OTA_SYSTEMCTL:-systemctl}"
# The fleet agent's time limit for this whole job - bridge-agent.py DETACHED["update"], the same
# number. One hour used to cover the download AND the slot write: at ~3 Mbit/s the download alone
# takes ~50 minutes, systemd killed the job half-way through writing the slot, and the rollout
# counted a rollback (2026-09-28). Now the download and any wait for a meeting to end must be over
# before the last WRITE_RESERVE_S, which the slot write (hash, mkfs, unpacking ~3.5 GB, fsck) keeps
# for itself. An update that runs out of time stops BEFORE the write; installing it again resumes
# the download.
BUDGET_S="${BRIDGE_OTA_BUDGET_S:-10800}"
WRITE_RESERVE_S=1800
TRYBOOT_WAIT_S="${BRIDGE_OTA_TRYBOOT_WAIT_S:-86400}"   # a staged slot waits at most a day for a quiet moment
POLL_S="${BRIDGE_OTA_POLL_S:-60}"
PROGRESS_S="${BRIDGE_OTA_PROGRESS_S:-30}"
T0=$(date +%s)
LATER="try again after the meeting (or install with the “even if a presenter is live” option)"
MOUNTED="" PHASE=""

log(){ echo "[ota] $*"; }
# "boot" says which boot wrote the status, so the next boot can tell an update that a restart cut
# short from one that is still running (bridge-ab healthcheck).
status(){ mkdir -p "$STAGE"; printf '{"state":"%s","version":"%s","detail":"%s","ts":%s,"boot":"%s"}\n' \
            "$1" "${VER:-}" "$(printf '%s' "${2:-}" | tr '"\\\t\n\r' "''   ")" "$(date +%s)" "$(cat "$BOOT_ID" 2>/dev/null)" \
            > "$STATUS.tmp" && mv -f "$STATUS.tmp" "$STATUS"; }
# The message alone reaches status.json, the panel and the alert email. (Until 2026-09-28 every
# argument was joined, so the exit code became part of the text: "mkfs failed on … 6".)
die(){ echo "[ota] ERROR: $1" >&2; status failed "$1"; exit "${2:-1}"; }
# The same, without touching status.json: for a refusal that must not overwrite the status of an
# update that is already running.
refuse(){ echo "[ota] ERROR: $1" >&2; exit "${2:-1}"; }
# Forget a staged slot: nothing may trial-boot or auto-commit it any more.
disarm(){ rm -f "$BOOT/.ota-autocommit" "$STAGE/trial-version"; sync; }
dur(){   # dur <seconds> -> "2 h 30 min" / "30 min" / "40 s"
  local h=$(( $1 / 3600 )) m=$(( $1 % 3600 / 60 ))
  if [ "$h" -gt 0 ] && [ "$m" -gt 0 ]; then echo "$h h $m min"
  elif [ "$h" -gt 0 ]; then echo "$h h"
  elif [ "$m" -gt 0 ]; then echo "$m min"
  else echo "$1 s"; fi
}

# busy: prints why the bridge must not be interrupted now and succeeds; fails when it is free.
busy(){
  local f pid t0 t1
  for f in $UDC_GLOB; do
    case "$(cat "$f" 2>/dev/null)" in configured|suspended) echo "the meeting laptop is attached"; return 0 ;; esac
  done
  if [ -n "$LIVE_CMD" ]; then eval "$LIVE_CMD" && { echo "a presenter session is live"; return 0; }; return 1; fi
  pid=$(pgrep -f 'udpsrc port=5000' | head -1)
  [ -n "$pid" ] || return 1
  t0=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null); sleep 2
  t1=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null)
  [ "${t1:-0}" -gt "${t0:-0}" ] && { echo "a presenter session is live"; return 0; }
  return 1
}
# wait_for_quiet <max-seconds> <what waits>: returns once no meeting is in progress (at once with
# --force); fails when <max-seconds> pass first, leaving the reason in $WHY. The check is a file
# read and a 2-second CPU sample once a minute - nothing a live bridge notices.
wait_for_quiet(){
  local max="$1" what="$2" start last=""
  WHY=""
  [ -n "$FORCE" ] && return 0
  start=$(date +%s)
  while WHY="$(busy)"; do
    [ $(( $(date +%s) - start )) -lt "$max" ] || return 1
    if [ "$WHY" != "$last" ]; then        # one status write per change, not one a minute
      status waiting "$what once the meeting is over ($WHY)"; log "waiting: $WHY"; last="$WHY"
    fi
    sleep "$POLL_S"
  done
  WHY=""
  return 0
}
# trial_boot_when_quiet <slot>: the reboot into the new slot. The meeting check at the start ran
# before a download that can take hours; a presenter may have gone live since, and rebooting takes
# the room's camera and microphone away for minutes (2026-09-28). So the trial boot waits for a
# quiet moment - up to TRYBOOT_WAIT_S, then it is called off and the slot disarmed.
trial_boot_when_quiet(){
  PHASE=trial-wait
  if ! wait_for_quiet "$TRYBOOT_WAIT_S" "slot $1 holds $VER; the bridge restarts into it"; then
    disarm
    die "slot $1 holds $VER, but the bridge was not free for $(dur "$TRYBOOT_WAIT_S") ($WHY), so the restart into it was called off — install the update again after the meeting" 8
  fi
  status rebooting "trial boot of slot $1"
  log "rebooting into a trial of slot $1 (auto-commit if healthy, auto-rollback if not)"
  trap - TERM INT HUP
  exec bridge-ab tryboot "$1"
}
# systemd stops the job with SIGTERM when its time limit runs out, or when the bridge shuts down.
# Say so and leave nothing mounted (2026-09-28): the status used to stay "writing" for good, with
# the standby slot still mounted.
stopped(){
  trap - TERM INT HUP
  [ -n "$MOUNTED" ] && umount "$MNT" 2>/dev/null
  [ "$PHASE" = trial-wait ] && disarm
  die "the update was stopped before it finished (its time limit ran out, or the bridge shut down) — install it again" 10
}

SRC="" VER_ARG="" FORCE="" NOREBOOT="${OTA_NO_REBOOT:-0}" FLEET="" VER="" TRYBOOT_SLOT=""
two(){ [ $# -ge 2 ] && echo 2 || echo 1; }   # an option missing its value must not loop forever
while [ $# -gt 0 ]; do case "$1" in
  --version)   VER_ARG="${2:-}"; shift "$(two "$@")" ;;
  --url)       SRC="${2:-}"; shift "$(two "$@")" ;;
  --tryboot-when-idle) TRYBOOT_SLOT="${2:-}"; shift "$(two "$@")" ;;
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
case "$0" in /*) SELF="$0" ;; *) SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" ;; esac

# Tests: show where it would fetch from and stop, before anything needs root.
[ -n "${BRIDGE_OTA_DRYRUN:-}" ] && { echo "source=$SRC force=${FORCE:-0} fleet=${FLEET:-0} noreboot=$NOREBOOT"; exit 0; }
[ "$(id -u)" = 0 ] || refuse "must run as root" 1

# One OS update at a time (2026-09-28). Every fleet command runs as its own systemd job, so an
# "Install on…" test and a rollout reaching the same bridge ran two copies at once: both appended
# to one download, and one's mkfs met the other's extraction. The trial-boot wait holds it too.
exec 9>"$LOCK" || refuse "cannot open the update lock $LOCK" 9
flock -n 9 || refuse "another OS update is already running on this bridge (or waiting to trial-boot)" 9
trap stopped TERM INT HUP

if [ -n "$TRYBOOT_SLOT" ]; then
  case "$TRYBOOT_SLOT" in A|B) ;; *) refuse "bad slot '$TRYBOOT_SLOT'" 64 ;; esac
  VER="$(cat "$STAGE/trial-version" 2>/dev/null)"
  if [ -z "$VER" ] || [ ! -e "$BOOT/.ota-autocommit" ]; then
    log "nothing is staged for a trial boot any more — not rebooting"; exit 0
  fi
  trial_boot_when_quiet "$TRYBOOT_SLOT"
fi
# Between staging and the trial boot the lock is free for 45 s; the scheduled job is the sign.
"$SYSTEMCTL" is-active --quiet bridge-ota-tryboot.timer bridge-ota-tryboot.service 2>/dev/null \
  && refuse "an OS update is already staged on this bridge and about to trial-boot" 9

[ -n "$SRC" ] || die "no update source (--version, --url, or BRIDGE_UPDATE_URL)" 2
[ -f "$PUBKEY" ] || die "this bridge has no OTA public key, so it cannot verify an update" 2
for t in zstd tar openssl curl; do command -v "$t" >/dev/null || die "a tool the update needs is missing: $t" 2; done
[ -x "$MKFS" ] || MKFS="$(command -v mkfs.ext4)" || die "a tool the update needs is missing: mkfs.ext4" 2

# An update ends in two reboots. Never start one under a live meeting unless told to.
if [ -z "$FORCE" ] && WHY="$(busy)"; then die "$WHY — $LATER" 8; fi

# pick_standby: sets st/dev/puuid for the slot that is not committed, refusing the running one.
pick_standby(){
  local running m
  # HARD SAFETY: never touch the running root. `findmnt /` alone never matched on the read-only
  # image: under overlayroot the source of / is "overlayroot" (the real device sits at
  # /media/root-ro), so during a trial boot that is not committed yet - when the standby, "not
  # committed" slot IS the running one - it passed, and only mke2fs refusing a mounted device
  # stood between an update and the running system (2026-09-28). A trial boot is refused outright
  # (its own health check is about to commit it or roll it back), and the kernel command line
  # names the running root whatever is mounted on top of it.
  grep -q 'bridge_tryboot=1' "$PROC_CMDLINE" 2>/dev/null \
    && die "the bridge is running a trial of a new system that is not committed yet — install again once it has committed or rolled back" 6
  st=$(bridge-ab status | sed -n 's/^standby slot : \([AB]\).*/\1/p'; true)
  case "$st" in
    A) dev=/dev/mmcblk0p2; puuid=0d18cc81-02 ;;
    B) dev=/dev/mmcblk0p3; puuid=0d18cc81-03 ;;
    *) die "could not tell which slot is the standby slot (bridge-ab said '$st')" 5 ;;
  esac
  running=$(sed -n 's/.*root=PARTUUID=\([0-9a-fA-F-]*\).*/\1/p' "$PROC_CMDLINE" 2>/dev/null)
  [ -n "$running" ] || die "could not tell which slot is running — not writing either one" 6
  [ "$running" != "$puuid" ] || die "slot $st is the running system — not writing it" 6
  for m in / /media/root-ro; do
    [ "$(findmnt -no SOURCE "$m" 2>/dev/null)" != "$dev" ] || die "slot $st ($dev) is mounted at $m — not writing it" 6
  done
  [ "$(lsblk -no PARTUUID "$dev")" = "$puuid" ] \
    || die "the standby slot ($dev) does not carry its expected partition id — not writing it" 6
}
pick_standby          # refuse now, not after hours of downloading

mkdir -p "$STAGE"
free_mb=$(df -Pm "$STAGE" | awk 'NR==2 {print $4}')
[ "${free_mb:-0}" -ge "$MIN_FREE_MB" ] || die "only ${free_mb:-?} MB free on /data (need $MIN_FREE_MB)" 2

# fetch <relpath> <dest> [seconds] — resumable and retried: this runs over venue Wi-Fi. curl's own
# result counts (2026-09-28): only the file being non-empty used to, so a download that gave up
# half-way "succeeded", failed the hash check and was deleted, and the next attempt started from
# zero instead of resuming. With [seconds] it is cut off after that long (status 124), keeping
# what it has.
fetch(){
  local lim="" rc
  [ -n "${3:-}" ] && command -v timeout >/dev/null 2>&1 && lim="timeout $3"
  case "$SRC" in
    http://*|https://*|file://*)
      $lim curl -fSL --retry 8 --retry-delay 5 --retry-all-errors --connect-timeout 20 \
           -C - -o "$2" "$SRC/$1" 2>&1 | tail -2; rc=${PIPESTATUS[0]} ;;
    *) $lim cp "$SRC/$1" "$2"; rc=$? ;;
  esac
  [ "$rc" = 0 ] || return "$rc"
  [ -s "$2" ]
}
got_mb(){
  local b
  b=$(stat -c %s "$STAGE/$img" 2>/dev/null || stat -f %z "$STAGE/$img" 2>/dev/null || echo 0)
  echo "$(( b / 1048576 )) MB${size:+ of $(( size / 1048576 )) MB}"
}
# Download progress for the panel: a 1.1 GB image over venue Wi-Fi can take hours, and
# "downloading" alone never said whether it was moving (2026-09-28). Stopped with TERM, it ends
# between two status writes, never half-way through one.
progress(){
  local s=""
  trap '[ -n "$s" ] && kill "$s" 2>/dev/null; exit 0' TERM
  while :; do
    sleep "$PROGRESS_S" & s=$!
    wait "$s"
    status downloading "$img: $(got_mb)"
  done
}

status fetching "manifest from $SRC"
rm -f "$STAGE/manifest.txt" "$STAGE/manifest.txt.sig"
log "fetching manifest + signature from $SRC"
fetch manifest.txt "$STAGE/manifest.txt" || die "could not download the update's manifest" 3
fetch manifest.txt.sig "$STAGE/manifest.txt.sig" || die "could not download the manifest's signature" 3
log "verifying manifest signature (EC/SHA256) ..."
openssl dgst -sha256 -verify "$PUBKEY" -signature "$STAGE/manifest.txt.sig" "$STAGE/manifest.txt" >/dev/null 2>&1 \
  || die "the manifest signature does not verify with this bridge's OTA key — refusing the update" 3
log "  signature OK"
# The disk image and rootfs manifests share a signing key. A valid signature alone
# must never make an unrelated product eligible for partition formatting.
product=$(sed -n 's/^product=//p' "$STAGE/manifest.txt")
[ "$product" = netbridge-os ] || die "the signed manifest is not a NetBridge OS root filesystem update" 3

VER=$(sed -n 's/^version=//p'  "$STAGE/manifest.txt")
img=$(sed -n 's/^image=//p'    "$STAGE/manifest.txt")
want=$(sed -n 's/^sha256=//p'  "$STAGE/manifest.txt")
kern=$(sed -n 's/^kernel=//p'  "$STAGE/manifest.txt")
size=$(sed -n 's/^size=//p'    "$STAGE/manifest.txt"); case "$size" in *[!0-9]*) size="" ;; esac
case "$VER" in *[!A-Za-z0-9.+-]*) VER=""; die "the manifest names an invalid version" 3 ;; esac
[ -n "$VER" ] && [ -n "$img" ] && [ -n "$want" ] || die "the update's manifest is incomplete" 3
case "$img" in */*|.*) die "the update's manifest names an invalid image file" 3 ;; esac
[ -n "$VER_ARG" ] && [ "$VER" != "$VER_ARG" ] && die "the manifest found is for $VER, not $VER_ARG" 3
log "  target version=$VER kernel=$kern"

# The download is kept between attempts so a retry resumes it - but only for the SAME image, so
# it is tied to the manifest's hash (2026-09-28). A leftover of another version used to be
# resumed as this one (its tail appended, or kept whole on a 416), fail the hash check after the
# whole download, and count as a rollback in a rollout.
if [ "$(cat "$STAGE/$img.sha256" 2>/dev/null)" != "$want" ]; then
  rm -f "$STAGE/$img"
  printf '%s\n' "$want" > "$STAGE/$img.sha256"
fi
status downloading "$img: $(got_mb)"
log "downloading $img (resumable) ..."
left=$(( T0 + BUDGET_S - WRITE_RESERVE_S - $(date +%s) )); [ "$left" -gt 0 ] || left=1
progress 9>&- & prog=$!     # without the lock: nothing it leaves behind may hold it
fetch "$img" "$STAGE/$img" "$left"; rc=$?
kill "$prog" 2>/dev/null; wait "$prog" 2>/dev/null
[ "$rc" = 124 ] && die "the download did not finish within $(dur "$left") ($(got_mb)), which leaves no time to write the new system — install the update again to resume the download" 4
[ "$rc" = 0 ] || die "the download stopped at $(got_mb) — install the update again to resume it" 4

# Everything from here is heavy - hashing 1.1 GB, then mkfs + unpacking at full CPU for several
# minutes, then the reboot - and the meeting check above ran before a download that can take
# hours (2026-09-28). Check again before the hash and once more right before the slot is
# formatted, and wait for the meeting to end - within the time left before the write's reserve.
quiet_or_stop(){
  wait_for_quiet $(( T0 + BUDGET_S - WRITE_RESERVE_S - $(date +%s) )) "$VER is downloaded and will be installed"     || die "$VER is downloaded, but the meeting did not end in time ($WHY) — install the update again after the meeting; the download is kept, so it is not downloaded again" 8
}
quiet_or_stop
# The heavy steps run at the lowest CPU priority, so a presenter who goes live part-way keeps the
# processor for the meeting.
got=$(nice -n 19 sha256sum "$STAGE/$img" | cut -d' ' -f1)
if [ "$got" != "$want" ]; then
  rm -f "$STAGE/$img" "$STAGE/$img.sha256"
  die "the downloaded image does not match its signed manifest (sha256 ${got:0:12}…, expected ${want:0:12}…) — deleted; install the update again" 4
fi
log "  image hash OK ($got)"

if [ -n "$kern" ] && [ "$kern" != "$(uname -r)" ]; then
  log "  WARN: image kernel ($kern) != running kernel ($(uname -r)); the shared boot partition is not updated by this flow"
fi

quiet_or_stop         # the hash took a minute or two: a presenter may have gone live since
pick_standby          # again: the download and the wait may have taken hours
status writing "standby slot $st"
log "writing standby slot $st ($dev, PARTUUID=$puuid)"

mkdir -p "$MNT" || die "cannot create standby mountpoint; standby slot left untouched" 6
umount "$MNT" 2>/dev/null || true
"$MKFS" -F -q -L "root$st" "$dev" || die "could not format the standby slot ($dev)" 6
mount "$dev" "$MNT" || die "could not mount the standby slot ($dev)" 6
MOUNTED=1
log "  extracting root filesystem ..."
if ! nice -n 19 zstd -d -q --long=31 -c "$STAGE/$img" | nice -n 19 tar -xf - -C "$MNT" --numeric-owner --acls --xattrs; then
  umount "$MNT"; MOUNTED=""; die "could not unpack the new system onto slot $st" 6
fi
sync

# normalize standby: its fstab '/' line must use its OWN PARTUUID
awk -v p="$puuid" 'BEGIN{d=0}
  /[ \t]\/[ \t].*ext4/ && !d { sub(/PARTUUID=[0-9a-fA-F-]+/, "PARTUUID=" p); d=1 }
  {print}' "$MNT/etc/fstab" > "$MNT/etc/fstab.$$" && mv "$MNT/etc/fstab.$$" "$MNT/etc/fstab"
# It must really be a bridge root before we boot it: identity binds, read-only overlay, the
# A/B switch, the agent. A payload without them could never pass the trial — only cost a reboot.
bad=""
grep -q "/data/tailscale" "$MNT/etc/fstab" || bad="$bad /data-binds"
[ -f "$MNT/etc/overlayroot.conf" ] || bad="$bad overlayroot"
[ -x "$MNT/usr/local/bin/bridge-ab" ] || bad="$bad bridge-ab"
[ -f "$MNT/usr/local/bin/bridge-agent.py" ] || bad="$bad agent"
echo "$VER" > "$MNT/etc/netbridge-image-version"
umount "$MNT"; MOUNTED=""
nice -n 19 "$FSCK" -p -f "$dev" >/dev/null 2>&1 || true
[ -z "$bad" ] || die "the new system is not a complete bridge system (missing:$bad) — not booting it" 6
log "  standby fstab / -> PARTUUID=$puuid; bridge root checks OK"
rm -f "$STAGE/$img" "$STAGE/$img.sha256"    # 1.1 GB back to /data; the slot holds it now

: > "$BOOT/.ota-autocommit"
echo "$VER" > "$STAGE/trial-version"
sync                  # both on the card before anything says "staged" (the boot partition is vfat)
if [ "$NOREBOOT" = 1 ]; then
  status staged "slot $st holds $VER; run: sudo bridge-ab tryboot $st"
  log "STAGED slot $st with version $VER (auto-commit armed). Not rebooting."
  log "  inspect it, then run:  sudo bridge-ab tryboot $st"
  exit 0
fi
if [ -n "$FLEET" ]; then
  # The trial boot runs from its own systemd job 45 s from now, so this command reports "staged"
  # first - and through this script, so it happens only once no meeting is in progress.
  # shellcheck disable=SC2086  # ${FORCE:+--force} is one word or none
  systemd-run --unit=bridge-ota-tryboot --collect --on-active=45 "$SELF" --tryboot-when-idle "$st" ${FORCE:+--force} >/dev/null 2>&1 \
    || { disarm; die "could not schedule the trial boot" 7; }
  status staged "slot $st holds $VER; the bridge restarts into it in 45 s, or once the meeting is over (it keeps the new system only if it comes up healthy)"
  log "STAGED $VER in slot $st — trial boot in 45 s (later if a meeting is in progress); it commits itself if healthy, rolls back if not"
  exit 0
fi
trial_boot_when_quiet "$st"
