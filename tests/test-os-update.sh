#!/bin/bash
# Whole-OS updates (2026-09-28): the updater (bridge-update.sh) and the A/B switch (bridge-ab),
# run for real WITHOUT a Pi.
#
# Real openssl keys and signatures, a real signed manifest and zstd root filesystem, real curl
# (file://) downloads with resume; stand-ins only for what needs a Pi: mkfs/mount/fsck, the slot
# table, systemd, the UDC state, "is a presenter live", the boot id. bridge-ab runs as a copy
# with its device paths moved into the sandbox (the copy is checked to contain no device path).
#
#   bash tests/test-os-update.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE/.."
T="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); }
for t in zstd openssl curl python3; do command -v "$t" >/dev/null || { echo "  SKIPPED - $t not installed"; exit 0; }; done
REAL_ZSTD="$(command -v zstd)"

mkdir -p "$T/bin" "$T/boot" "$T/stage" "$T/mnt" "$T/srv" "$T/log" "$T/ab"
: > "$T/log/actions"; : > "$T/log/sync"

# ---- stand-ins for the Pi --------------------------------------------------------------------
stub(){ printf '#!/bin/bash\n%s\n' "$2" > "$T/bin/$1"; chmod +x "$T/bin/$1"; }
stub id          '[ "${1:-}" = -u ] && { echo 0; exit 0; }; exec /usr/bin/id "$@"'
stub systemd-run "echo \"systemd-run \$*\" >> $T/log/actions; [ ! -e $T/sdrun-fail ]"
stub mount       "echo \"mount \$*\" >> $T/log/actions"
stub umount      "echo \"umount \$*\" >> $T/log/actions"
stub findmnt     '[ "${3:-}" = / ] && echo overlayroot; exit 0'
stub lsblk       'case "${3:-}" in *p2) echo 0d18cc81-02 ;; *p3) echo 0d18cc81-03 ;; esac'
stub df          'echo "Filesystem 1M-blocks Used Available Capacity Mounted"; echo "data 99999 1 99998 1% /data"'
stub logger      "echo \"logger \$*\" >> $T/log/actions"
stub reboot      "echo \"reboot \$*\" >> $T/log/actions"
stub tailscale   'exit 0'
stub e2fsck      'exit 0'
# mkfs "formats" the sandbox slot (mount is a no-op, so the slot IS $T/mnt)
stub mkfs.ext4   "echo \"mkfs \$*\" >> $T/log/actions; [ -e $T/mkfs-fail ] && exit 1; rm -rf $T/mnt/* $T/mnt/.[!.]*; exit 0"
# sync records what cmdline.txt held at that moment, and whether the new copy existed
stub sync        "echo \"sync cmdline=\$(cat $T/boot/cmdline.txt 2>/dev/null) new=\$([ -e $T/boot/cmdline.txt.new ] && echo yes || echo no)\" >> $T/log/sync"
# systemctl: services are active unless $T/unhealthy; the trial-boot job exists while $T/tryboot-pending
stub systemctl   "case \"\$1\" in
  is-active) shift; [ \"\$1\" = --quiet ] && shift
    case \"\$1\" in bridge-ota-tryboot*) [ -e $T/tryboot-pending ]; exit ;; esac
    [ -e $T/unhealthy ] && { echo failed; exit 3; }; echo active ;;
  *) echo \"systemctl \$*\" >> $T/log/actions ;;
esac"
# bridge-ab as the updater sees it: the slot table, and the trial boot it hands over to
stub bridge-ab   "case \"\$1\" in
  status) echo \"standby slot : \$(cat $T/standby)  (PARTUUID=x)\" ;;
  tryboot) echo \"bridge-ab tryboot \$2\" >> $T/log/actions ;;
esac"
cat > "$T/bin/flock" <<'EOF'
#!/usr/bin/env python3
# flock [-n] FD: the lock belongs to the open file the caller keeps, exactly like util-linux flock
import fcntl, sys
a = sys.argv[1:]
try:
    fcntl.flock(int([x for x in a if x.isdigit()][-1]), fcntl.LOCK_EX | (fcntl.LOCK_NB if "-n" in a else 0))
except OSError:
    sys.exit(1)
EOF
chmod +x "$T/bin/flock"
if ! command -v timeout >/dev/null; then     # coreutils timeout, for a Mac
  cat > "$T/bin/timeout" <<'EOF'
#!/usr/bin/env python3
import subprocess, sys
p = subprocess.Popen(sys.argv[2:])
try:
    sys.exit(p.wait(timeout=float(sys.argv[1])))
except subprocess.TimeoutExpired:
    p.terminate(); p.wait(); sys.exit(124)
EOF
  chmod +x "$T/bin/timeout"
fi
command -v sha256sum >/dev/null || stub sha256sum 'shasum -a 256 "$@"'
export PATH="$T/bin:$PATH"

# ---- signed OS versions on a "fleet" -----------------------------------------------------------
openssl ecparam -name prime256v1 -genkey -noout -out "$T/ota.pem" 2>/dev/null
openssl ec -in "$T/ota.pem" -pubout -out "$T/ota-pub.pem" 2>/dev/null
fsize(){ python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$1"; }
publish(){  # publish <version> <filler-bytes>: a bridge root filesystem + signed manifest
  local r="$T/roots/$1" d="$T/srv/$1"
  mkdir -p "$r/etc" "$r/usr/local/bin" "$d"
  printf 'PARTUUID=0d18cc81-02  /  ext4  defaults,noatime  0  1\n/data/tailscale /var/lib/tailscale none bind 0 0\n' > "$r/etc/fstab"
  echo 'overlayroot="tmpfs:recurse=0"' > "$r/etc/overlayroot.conf"
  printf '#!/bin/bash\n' > "$r/usr/local/bin/bridge-ab"; chmod +x "$r/usr/local/bin/bridge-ab"
  echo '# agent' > "$r/usr/local/bin/bridge-agent.py"
  head -c "$2" /dev/urandom > "$r/filler"
  tar -C "$r" -cf - . | zstd -q -c > "$d/rootfs.tar.zst"
  printf 'version=%s\nimage=rootfs.tar.zst\nsha256=%s\nsize=%s\nkernel=%s\n' "$1" \
    "$(sha256sum "$d/rootfs.tar.zst" | cut -d' ' -f1)" "$(fsize "$d/rootfs.tar.zst")" "$(uname -r)" > "$d/manifest.txt"
  openssl dgst -sha256 -sign "$T/ota.pem" -out "$d/manifest.txt.sig" "$d/manifest.txt" 2>/dev/null
}
publish 2.2.1-aaaaaaa 300000
publish 2.2.2-bbbbbbb 500000
publish 2.2.3-ccccccc 3300000       # big enough to show progress in whole MB
V1=2.2.1-aaaaaaa; V2=2.2.2-bbbbbbb; V3=2.2.3-ccccccc

export BRIDGE_OTA_PUBKEY="$T/ota-pub.pem" BRIDGE_OTA_STAGE="$T/stage" BRIDGE_OTA_BOOT="$T/boot" \
       BRIDGE_OTA_MNT="$T/mnt" BRIDGE_OTA_MKFS="$T/bin/mkfs.ext4" BRIDGE_OTA_FSCK="$T/bin/e2fsck" \
       BRIDGE_OTA_PROC_CMDLINE="$T/proc-cmdline" BRIDGE_OTA_BOOT_ID="$T/boot_id" BRIDGE_OTA_UDC_GLOB="$T/udc" \
       BRIDGE_OTA_LOCK="$T/lock" BRIDGE_OTA_SYSTEMCTL="$T/bin/systemctl" BRIDGE_OTA_POLL_S=0.1
UPD="$REPO/pi/scripts/bridge-update.sh"
CMD_A="console=serial0,115200 console=tty1 root=PARTUUID=0d18cc81-02 rootfstype=ext4 fsck.repair=yes rootwait modules-load=dwc2"
CMD_B="${CMD_A/0d18cc81-02/0d18cc81-03}"

fresh(){    # a bridge running slot A (committed), idle, nothing staged
  rm -rf "$T/stage" "$T/mnt" "$T/boot"; mkdir -p "$T/stage" "$T/mnt" "$T/boot"
  echo "$CMD_A" > "$T/boot/cmdline.txt"; printf 'arm_64bit=1\ncmdline=cmdline.txt\n' > "$T/boot/config.txt"
  echo "$CMD_A" > "$T/proc-cmdline"; echo B > "$T/standby"; echo "boot-1" > "$T/boot_id"
  echo "not attached" > "$T/udc"; : > "$T/live"
  rm -f "$T/mkfs-fail" "$T/unhealthy" "$T/tryboot-pending" "$T/meeting-over" "$T/sdrun-fail"
  : > "$T/log/actions"; : > "$T/log/sync"
}
# update <args...>: run the real updater; "is a presenter live" is $LIVE (default: $T/live non-empty)
update(){ BRIDGE_OTA_LIVE_CMD="${LIVE:-[ -s $T/live ]}" bash "$UPD" "$@" >"$T/out" 2>&1; echo $? > "$T/rc"; }
rc(){ cat "$T/rc"; }
st(){ sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" "$T/stage/status.json" 2>/dev/null; }
did(){ grep -q -- "$1" "$T/log/actions"; }
staged_ok(){ [ "$(rc)" = 0 ] && [ "$(st state)" = staged ]; }
wait_for(){ local i; for i in $(seq 1 100); do eval "$1" && return 0; sleep 0.1; done; return 1; }
# The update lock is free again. A job killed here can leave a child behind (a stand-in's sleep)
# that still holds it; on a bridge systemd stops every process of the job at once.
lock_free(){ python3 -c 'import fcntl,sys; fcntl.flock(open(sys.argv[1],"a"), fcntl.LOCK_EX|fcntl.LOCK_NB)' "$T/lock" 2>/dev/null; }

echo; echo "bridge-update.sh"
echo "================"
# ===================== messages: no exit code in the text, no CLI jargon =====================
fresh; echo configured > "$T/udc"; update --url "file://$T/srv/$V1"
d="$(st detail)"
if [ "$(rc)" = 8 ] && echo "$d" | grep -q "meeting laptop is attached" && echo "$d" | grep -q "try again after the meeting" \
   && ! echo "$d" | grep -qE ' [0-9]+$' && ! echo "$d" | grep -q -- "--force"; then
  ok "refused under an attached laptop, in plain words: '$d'"
else no "refusal text (rc $(rc)): '$d'"; fi
fresh; touch "$T/mkfs-fail"; update --no-reboot --url "file://$T/srv/$V1"
d="$(st detail)"
[ "$(rc)" = 6 ] && [ "$(st state)" = failed ] && ! echo "$d" | grep -qE ' [0-9]+$' \
  && ok "a failure's status carries the message only, not the exit code: '$d'" || no "failure detail (rc $(rc)): '$d'"

# ===================== never the running slot, even under overlayroot =====================
fresh; echo "$CMD_B bridge_tryboot=1" > "$T/proc-cmdline"; echo B > "$T/standby"   # uncommitted trial of B
update --no-reboot --url "file://$T/srv/$V1"
if [ "$(rc)" = 6 ] && ! did "mkfs" && echo "$(st detail)" | grep -q "trial of a new system that is not committed" \
   && [ ! -e "$T/stage/rootfs.tar.zst" ]; then
  ok "during a trial boot (slot B running, not committed) the update is refused before anything is downloaded"
else no "wrote or downloaded during a trial boot (rc $(rc), mkfs: $(grep -c mkfs "$T/log/actions"), detail '$(st detail)')"; fi
fresh; echo "$CMD_B" > "$T/proc-cmdline"; echo B > "$T/standby"      # B runs, but the slot table calls B "standby"
update --no-reboot --url "file://$T/srv/$V1"
[ "$(rc)" = 6 ] && ! did "mkfs" && echo "$(st detail)" | grep -q "slot B is the running system" \
  && ok "the running root is recognised from the kernel command line (findmnt / only says 'overlayroot')" \
  || no "the running slot was not recognised (rc $(rc), '$(st detail)')"

# ===================== a leftover download of another version =====================
fresh; cp "$T/srv/$V1/rootfs.tar.zst" "$T/stage/rootfs.tar.zst"            # left by an interrupted 2.2.1
update --no-reboot --url "file://$T/srv/$V2"
if staged_ok && [ "$(cat "$T/mnt/etc/netbridge-image-version" 2>/dev/null)" = "$V2" ]; then
  ok "a leftover 2.2.1 download does not poison the 2.2.2 update (fresh download, staged)"
else no "leftover of another version broke the update (rc $(rc)): $(tail -2 "$T/out")"; fi
[ ! -e "$T/stage/rootfs.tar.zst" ] && [ ! -e "$T/stage/rootfs.tar.zst.sha256" ] \
  && ok "after the slot is written the download and its record are gone" || no "staging files left behind"
fresh
sed -n 's/^sha256=//p' "$T/srv/$V2/manifest.txt" > "$T/stage/rootfs.tar.zst.sha256"
head -c 100000 "$T/srv/$V2/rootfs.tar.zst" > "$T/stage/rootfs.tar.zst"          # half of the SAME image
update --no-reboot --url "file://$T/srv/$V2"
staged_ok && ok "a partial download of the same image still resumes" || no "same-image resume broke (rc $(rc))"

# ===================== a download that stops is kept for the next attempt =====================
fresh; sed -n 's/^sha256=//p' "$T/srv/$V1/manifest.txt" > "$T/stage/rootfs.tar.zst.sha256"
head -c 100000 "$T/srv/$V1/rootfs.tar.zst" > "$T/stage/rootfs.tar.zst"
mv "$T/srv/$V1/rootfs.tar.zst" "$T/srv/$V1/rootfs.hidden"                     # the fleet stops serving it
mkdir -p "$T/noretry"                       # the same curl, minus 8 retries x 5 s of waiting
cat > "$T/noretry/curl" <<'EOF2'
#!/bin/bash
a=(); while [ $# -gt 0 ]; do case "$1" in --retry|--retry-delay) shift 2 ;; --retry-all-errors) shift ;; *) a+=("$1"); shift ;; esac; done
exec /usr/bin/curl "${a[@]}"
EOF2
chmod +x "$T/noretry/curl"
PATH="$T/noretry:$PATH" update --no-reboot --url "file://$T/srv/$V1"
mv "$T/srv/$V1/rootfs.hidden" "$T/srv/$V1/rootfs.tar.zst"
if [ "$(rc)" = 4 ] && [ "$(fsize "$T/stage/rootfs.tar.zst" 2>/dev/null)" = 100000 ] && echo "$(st detail)" | grep -q "resume"; then
  ok "a download that stops half-way is kept (not failed as a hash mismatch and deleted)"
else no "stopped download: rc $(rc), kept $(fsize "$T/stage/rootfs.tar.zst" 2>/dev/null || echo 0) bytes, '$(st detail)'"; fi

# ===================== the time limit: never start the slot write without time for it =====================
# The agent kills the job after BUDGET_S; the last 30 minutes belong to the write. A download that
# is still going when only that is left stops there, keeps what it has, and nothing is written.
fresh; mkdir -p "$T/slowcp"
printf '#!/bin/bash\ncase "$1" in *rootfs*) head -c 60000 "$1" > "$2"; sleep 6 ;; esac\nexec /bin/cp "$@"\n' > "$T/slowcp/cp"; chmod +x "$T/slowcp/cp"
PATH="$T/slowcp:$PATH" BRIDGE_OTA_BUDGET_S=1803 update --no-reboot "$T/srv/$V1"
if [ "$(rc)" = 4 ] && ! did "mkfs" && echo "$(st detail)" | grep -q "did not finish within" \
   && [ "$(fsize "$T/stage/rootfs.tar.zst" 2>/dev/null)" = 60000 ]; then
  ok "a download that runs into the write's reserve stops there: nothing written, the partial download kept ('$(st detail)')"
else no "download past the time limit: rc $(rc), mkfs $(grep -c mkfs "$T/log/actions"), '$(st detail)'"; fi
wait_for lock_free

# ===================== stopped by systemd (time limit, shutdown) while writing the slot =====================
fresh; mkdir -p "$T/slowz"
printf '#!/bin/bash\nsleep 4\nexec %s "$@"\n' "$REAL_ZSTD" > "$T/slowz/zstd"; chmod +x "$T/slowz/zstd"
PATH="$T/slowz:$PATH" BRIDGE_OTA_LIVE_CMD="[ -s $T/live ]" bash "$UPD" --no-reboot --url "file://$T/srv/$V1" >"$T/out" 2>&1 &
job=$!
if wait_for "grep -q '^mount ' $T/log/actions"; then
  sleep 0.5; kill -TERM "$job"; pkill -TERM -P "$job"      # systemd signals the whole job
fi
wait "$job"; echo $? > "$T/rc"
if [ "$(rc)" = 10 ] && [ "$(st state)" = failed ] && echo "$(st detail)" | grep -q "stopped before it finished" \
   && grep -q "^umount $T/mnt" "$T/log/actions" && [ ! -e "$T/boot/.ota-autocommit" ]; then
  ok "stopped while writing: the status says so (not 'writing' for good), the slot is unmounted, nothing armed"
else no "stopped mid-write: rc $(rc), state '$(st state)', '$(st detail)', actions $(tr '\n' '|' < "$T/log/actions")"; fi
wait_for lock_free

# ===================== a meeting that starts during the download =====================
fresh
MEETING="[ -s $T/stage/rootfs.tar.zst ] && [ ! -e $T/meeting-over ]"             # live once the image is down
BRIDGE_OTA_BUDGET_S=1802 LIVE="$MEETING" update --fleet --url "file://$T/srv/$V1"
if [ "$(rc)" = 8 ] && ! did "mkfs" && [ -s "$T/stage/rootfs.tar.zst" ] \
   && echo "$(st detail)" | grep -q "is downloaded, but the meeting did not end in time (a presenter session is live)"; then
  ok "presenter went live during the download: the slot is NOT written (no mkfs/unpack under a meeting), download kept"
else no "wrote the slot under a live meeting (rc $(rc), mkfs: $(grep -c mkfs "$T/log/actions"), '$(st detail)')"; fi
fresh; mkdir -p "$T/hashbin"                                                # ...or during the hash, after the check
printf '#!/bin/bash\ntouch %s/hashed\nexec %s "$@"\n' "$T" "$(command -v sha256sum)" > "$T/hashbin/sha256sum"; chmod +x "$T/hashbin/sha256sum"
rm -f "$T/hashed"; PATH="$T/hashbin:$PATH" BRIDGE_OTA_BUDGET_S=1802 LIVE="[ -e $T/hashed ]" update --no-reboot --url "file://$T/srv/$V1"
if [ "$(rc)" = 8 ] && [ -e "$T/hashed" ] && ! did "mkfs" && [ -s "$T/stage/rootfs.tar.zst" ]; then
  ok "presenter went live while the image was being checked: checked again right before formatting, nothing written"
else no "formatted the slot although a presenter had gone live (rc $(rc), mkfs: $(grep -c mkfs "$T/log/actions"), '$(st detail)')"; fi
fresh; cp "$T/srv/$V1/rootfs.tar.zst" "$T/stage/"; sed -n 's/^sha256=//p' "$T/srv/$V1/manifest.txt" > "$T/stage/rootfs.tar.zst.sha256"
touch "$T/meeting-over"; LIVE="$MEETING" update --fleet --url "file://$T/srv/$V1"
if [ "$(rc)" = 0 ] && grep -q -- "--tryboot-when-idle B" "$T/log/actions" && ! grep -q "bridge-ab tryboot" "$T/log/actions"; then
  ok "meeting over: staged, and the trial boot is scheduled through the meeting check (not a bare bridge-ab tryboot)"
else no "trial boot scheduling: rc $(rc), $(grep systemd-run "$T/log/actions")"; fi
[ "$(st state)" = staged ] && [ -e "$T/boot/.ota-autocommit" ] && [ "$(cat "$T/stage/trial-version")" = "$V1" ] \
  && ok "staged: auto-commit armed, trial version recorded" || no "staging state wrong ($(st state))"
grep -q "\"boot\":\"boot-1\"" "$T/stage/status.json" && ok "the status says which boot wrote it" || no "no boot id in status.json"

# the scheduled trial boot, while a meeting runs: waits, then is called off (and disarmed)
echo 1 > "$T/live"; : > "$T/log/actions"
BRIDGE_OTA_TRYBOOT_WAIT_S=1 update --tryboot-when-idle B
if [ "$(rc)" = 8 ] && ! did "bridge-ab tryboot" && [ ! -e "$T/boot/.ota-autocommit" ] && [ ! -e "$T/stage/trial-version" ] \
   && echo "$(st detail)" | grep -q "called off"; then
  ok "trial boot under a live meeting: never reboots; after the wait it is called off and disarmed"
else no "trial boot during a meeting: rc $(rc), actions '$(tr '\n' '|' < "$T/log/actions")', '$(st detail)'"; fi
# ...and when the meeting ends during the wait, it boots
: > "$T/boot/.ota-autocommit"; echo "$V1" > "$T/stage/trial-version"; echo 0 > "$T/n"
LIVE="n=\$(cat $T/n); echo \$((n+1)) > $T/n; [ \$n -lt 3 ]" update --tryboot-when-idle B
if [ "$(rc)" = 0 ] && did "bridge-ab tryboot B" && grep -q "waiting:" "$T/out" && [ "$(cat "$T/n")" -ge 3 ]; then
  ok "the meeting ends: the trial boot follows (after waiting)"
else no "trial boot after the meeting: rc $(rc), $(tail -2 "$T/out")"; fi
fresh; : > "$T/boot/.ota-autocommit"; echo "$V1" > "$T/stage/trial-version"; echo 1 > "$T/live"
update --tryboot-when-idle B --force
[ "$(rc)" = 0 ] && did "bridge-ab tryboot B" && ok "--force (\"even if a presenter is live\") does not wait" || no "--force waited (rc $(rc))"
fresh; update --tryboot-when-idle B
[ "$(rc)" = 0 ] && ! did "bridge-ab tryboot" && ok "nothing staged any more (disarmed): the scheduled job does not reboot" || no "rebooted with nothing staged (rc $(rc))"
# a waiting trial boot that systemd stops (e.g. the bridge shuts down): reported and disarmed
fresh; : > "$T/boot/.ota-autocommit"; echo "$V1" > "$T/stage/trial-version"; echo 1 > "$T/live"
BRIDGE_OTA_LIVE_CMD="[ -s $T/live ]" BRIDGE_OTA_TRYBOOT_WAIT_S=600 bash "$UPD" --tryboot-when-idle B >"$T/out" 2>&1 &
job=$!
wait_for "[ \"\$(st state)\" = waiting ]" && { kill -TERM "$job"; pkill -TERM -P "$job"; }
wait "$job"; echo $? > "$T/rc"
[ "$(rc)" = 10 ] && ! did "bridge-ab tryboot" && [ ! -e "$T/boot/.ota-autocommit" ] && [ ! -e "$T/stage/trial-version" ] \
  && [ "$(st state)" = failed ] && ok "a waiting trial boot that is stopped says so and disarms (no stale auto-commit)" \
  || no "stopped trial wait: rc $(rc), state '$(st state)', armed $([ -e "$T/boot/.ota-autocommit" ] && echo yes || echo no)"
wait_for lock_free
fresh; touch "$T/sdrun-fail"; update --fleet --url "file://$T/srv/$V1"
[ "$(rc)" = 7 ] && [ ! -e "$T/boot/.ota-autocommit" ] && [ ! -e "$T/stage/trial-version" ] \
  && ok "the trial boot cannot be scheduled: failed, and nothing is left armed" || no "schedule failure left it armed (rc $(rc))"

# ===================== one OS update at a time =====================
fresh; echo '{"state":"downloading","version":"2.2.1-aaaaaaa","detail":"x","ts":1}' > "$T/stage/status.json"
cp "$T/stage/status.json" "$T/status.before"
python3 -c 'import fcntl,sys,time; f=open(sys.argv[1],"w"); fcntl.flock(f, fcntl.LOCK_EX); open(sys.argv[2],"w").close(); time.sleep(30)' \
  "$T/lock" "$T/locked" & holder=$!
wait_for "[ -e $T/locked ]"
update --no-reboot --url "file://$T/srv/$V1"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
if [ "$(rc)" = 9 ] && ! did "mkfs" && cmp -s "$T/status.before" "$T/stage/status.json" && grep -q "already running" "$T/out"; then
  ok "a second update while one runs is refused, and the running one's status is left alone"
else no "concurrent update: rc $(rc), mkfs $(grep -c mkfs "$T/log/actions"), $(tail -1 "$T/out")"; fi
fresh; touch "$T/tryboot-pending"; update --no-reboot --url "file://$T/srv/$V1"
[ "$(rc)" = 9 ] && ! did "mkfs" && ok "an update staged and about to trial-boot blocks another (no rewrite of its slot)" || no "rewrote a staged slot (rc $(rc))"

# ===================== download progress for the panel =====================
fresh; mkdir -p "$T/slowbin"; : > "$T/log/seen"
printf '#!/bin/bash\ncase "$1" in *rootfs*) head -c 2200000 "$1" > "$2"; sleep 1.5; cat %s >> %s; echo >> %s ;; esac\nexec /bin/cp "$@"\n' \
  "$T/stage/status.json" "$T/log/seen" "$T/log/seen" > "$T/slowbin/cp"; chmod +x "$T/slowbin/cp"
PATH="$T/slowbin:$PATH" BRIDGE_OTA_PROGRESS_S=0.2 update --no-reboot "$T/srv/$V3"
grep -q '"state":"downloading","version":"2.2.3-ccccccc","detail":"rootfs.tar.zst: 2 MB of 3 MB"' "$T/log/seen" \
  && ok "while downloading, the status shows how far it got (2 MB of 3 MB)" || no "no download progress: $(tail -1 "$T/log/seen")"
staged_ok && ok "...and the update then goes on as normal" || no "progress broke the update (rc $(rc)): $(tail -2 "$T/out")"

echo; echo "bridge-ab"
echo "========="
sed -e "s#^BOOT=.*#BOOT=$T/boot#" -e "s#^STAGE=.*#STAGE=$T/stage#" -e "s#^PROC_CMDLINE=.*#PROC_CMDLINE=$T/proc-cmdline#" \
    -e "s#^BOOT_ID=.*#BOOT_ID=$T/boot_id#" -e "s#/sys/kernel/config/usb_gadget/g1/UDC#$T/gadget-udc#" \
    -e "s#/run/bridge-agent/last-ok#$T/last-ok#" -e "s#/run/bridge-ab-trial-ok#$T/trial-ok#" \
    -e 's/seq 1 60/seq 1 2/' -e 's/sleep 5$/sleep 0/' "$REPO/pi/scripts/bridge-ab" > "$T/ab/bridge-ab"
chmod +x "$T/ab/bridge-ab"
if grep -v '^[[:space:]]*#' "$T/ab/bridge-ab" | grep -qE '/boot/firmware|/data/|/proc/|/sys/|/run/'; then
  no "the sandbox copy of bridge-ab still names a device path — stopping before it touches one"
  echo; echo "  $pass passed, $fail failed"; exit 1
fi
AB(){ bash "$T/ab/bridge-ab" "$@" >"$T/out" 2>&1; }
echo configured > "$T/gadget-udc"; touch "$T/last-ok"
armed(){ [ -e "$T/boot/.ota-autocommit" ] || [ -e "$T/stage/trial-version" ] || [ -e "$T/boot/cmdline.tryboot" ] || [ -e "$T/boot/tryboot.txt" ]; }
stage_trial(){ : > "$T/boot/.ota-autocommit"; echo "$V1" > "$T/stage/trial-version"; }

# ===================== the trial cmdline falls back on a panic =====================
fresh; stage_trial; AB tryboot b
tc="$(cat "$T/boot/cmdline.tryboot" 2>/dev/null)"
if echo "$tc" | grep -q "root=PARTUUID=0d18cc81-03 " && echo "$tc" | grep -qE 'panic=10 bridge_tryboot=1$' \
   && [ "$(cat "$T/boot/cmdline.txt")" = "$CMD_A" ]; then
  ok "trial cmdline: slot B, panic=10 (a panicking trial reboots into the committed slot); committed cmdline untouched"
else no "trial cmdline: '$tc'"; fi
did "systemctl reboot --reboot-argument=0 tryboot" && grep -q "cmdline=cmdline.tryboot" "$T/boot/tryboot.txt" \
  && ok "one-shot tryboot reboot with a regenerated tryboot.txt" || no "tryboot reboot/config wrong"
[ "$(st state)" = rebooting ] && ok "the OS update's status says the trial boot started" || no "status after tryboot: $(st state)"
fresh; echo "$CMD_A panic=5" > "$T/boot/cmdline.txt"; AB tryboot B
[ "$(grep -o 'panic=[0-9]*' "$T/boot/cmdline.tryboot" | tr '\n' ' ')" = "panic=10 " ] && ok "an existing panic= is replaced, not duplicated" || no "panic entries: $(cat "$T/boot/cmdline.tryboot")"

# ===================== commit: a complete new cmdline, synced before and after the rename =====================
fresh; stage_trial; AB tryboot B; : > "$T/log/sync"; : > "$T/log/actions"
cp "$T/boot/cmdline.tryboot" "$T/proc-cmdline"; echo "boot-2" > "$T/boot_id"               # booted into the trial
AB healthcheck
if [ "$(cat "$T/boot/cmdline.txt")" = "$CMD_B" ] && [ ! -e "$T/boot/cmdline.txt.new" ]; then
  ok "healthy trial auto-commits: cmdline.txt = the old line with slot B's root, no trial marker, no panic="
else no "committed cmdline: '$(cat "$T/boot/cmdline.txt")'"; fi
if grep -q "sync cmdline=$CMD_A new=yes" "$T/log/sync" && grep -q "sync cmdline=$CMD_B new=no" "$T/log/sync"; then
  ok "the new cmdline is on the card before it replaces the old one, and synced after (a reset leaves one or the other)"
else no "cmdline commit not synced around the rename: $(tr '\n' '|' < "$T/log/sync")"; fi
[ "$(st state)" = committed ] && [ "$(st version)" = "$V1" ] && ! armed \
  && ok "status 'committed' for $V1, trial files and auto-commit cleared" || no "after commit: state $(st state), armed=$(armed && echo yes || echo no)"
# A committed cmdline carrying a stray trial marker (hand-edited, or older tooling) would make EVERY
# normal boot look like a trial - and an unhealthy moment then reboots it. Commit must strip it.
fresh; echo "$CMD_A bridge_tryboot=1" > "$T/boot/cmdline.txt"; cp "$T/boot/cmdline.txt" "$T/proc-cmdline"
AB commit
if [ "$(cat "$T/boot/cmdline.txt")" = "$CMD_A" ]; then
  ok "commit strips a stray trial marker from cmdline.txt (else every normal boot would look like a trial)"
else no "commit left: '$(cat "$T/boot/cmdline.txt")'"; fi

# ===================== a restart cut the trial short =====================
fresh; stage_trial; AB tryboot B                                            # status: rebooting, written in boot-1
echo "boot-2" > "$T/boot_id"                 # power cut: the firmware boots slot A normally
AB healthcheck
if [ "$(st state)" = "rolled back" ] && echo "$(st detail)" | grep -q "never finished" && [ "$(st version)" = "$V1" ] && ! armed; then
  ok "power cut during the trial: reported as rolled back ('$(st detail)'), auto-commit disarmed"
else no "power cut during trial: state '$(st state)', detail '$(st detail)', armed=$(armed && echo yes || echo no)"; fi
fresh; stage_trial; printf '{"state":"staged","version":"%s","detail":"x","ts":1,"boot":"boot-1"}\n' "$V1" > "$T/stage/status.json"
echo "boot-2" > "$T/boot_id"; AB healthcheck
[ "$(st state)" = failed ] && echo "$(st detail)" | grep -q "before the trial boot" && ! armed \
  && ok "restart between staging and the trial boot: reported as failed, disarmed" || no "restart before trial: '$(st state)' '$(st detail)'"
fresh; stage_trial; printf '{"state":"staged","version":"%s","detail":"x","ts":1,"boot":"boot-1"}\n' "$V1" > "$T/stage/status.json"
AB healthcheck                                                              # same boot: staging still pending
[ "$(st state)" = staged ] && [ -e "$T/boot/.ota-autocommit" ] && ok "staged in THIS boot: left alone (still armed)" || no "disarmed a pending staging"
fresh; printf '{"state":"writing","version":"%s","detail":"x","ts":1,"boot":"boot-1"}\n' "$V1" > "$T/stage/status.json"
echo "boot-2" > "$T/boot_id"; AB healthcheck
[ "$(st state)" = failed ] && echo "$(st detail)" | grep -q "while the update was being installed" && [ "$(st version)" = "$V1" ] \
  && ok "restart while the slot was being written: reported, version kept" || no "interrupted write: '$(st state)' '$(st detail)'"
fresh; echo "$V1" > "$T/stage/trial-version"; printf '{"state":"committed","version":"%s","detail":"x","ts":1}\n' "$V1" > "$T/stage/status.json"
echo "boot-2" > "$T/boot_id"; AB healthcheck
[ "$(st state)" = committed ] && [ ! -e "$T/stage/trial-version" ] && ok "a finished update's leftover trial-version is cleared, its result kept" || no "committed leftovers: $(st state)"

# ===================== a trial that never becomes healthy, or is rolled back by hand =====================
fresh; stage_trial; AB tryboot B; cp "$T/boot/cmdline.tryboot" "$T/proc-cmdline"; echo "boot-2" > "$T/boot_id"
touch "$T/unhealthy"; : > "$T/log/actions"; AB healthcheck
[ "$(st state)" = "rolled back" ] && did "^reboot" && ! armed && ok "unhealthy trial: rolled back, disarmed, rebooted" || no "failed trial: $(st state)"
fresh; stage_trial; AB tryboot B; cp "$T/boot/cmdline.tryboot" "$T/proc-cmdline"; echo "boot-2" > "$T/boot_id"; : > "$T/log/actions"
AB rollback
[ "$(st state)" = "rolled back" ] && echo "$(st detail)" | grep -q "by hand" && did "^reboot" && ! armed \
  && ok "rolled back by hand: reported, disarmed, rebooted" || no "manual rollback: state '$(st state)', armed=$(armed && echo yes || echo no)"

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
