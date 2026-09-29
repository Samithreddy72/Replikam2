#!/usr/bin/env bash
# Audit a built NetBridge image BEFORE it is flashed.
#
# WHY THIS EXISTS
# ---------------
# Five images in this project were un-bootable or silently missing a fix, and each one cost a
# flash, a walk to the hardware, and a power cycle to discover. Every one of those faults was
# visible in the image file itself. So: check the image on disk, where a mistake costs
# seconds, instead of on the card, where it costs an evening.
#
# WHY IT IS ONE PASS
# ------------------
# The first version ran ~50 separate greps over a 4GB mount and took ten minutes, which meant
# it got skipped. This walks the tree ONCE, builds an index, and answers every question from
# that index. 67 checks, well under a minute.
#
#   bash tools/image-audit.sh ~/Downloads/netbridge-os-*.img
set -uo pipefail
IMG="${1:?usage: image-audit.sh <path to .img>}"
[ -f "$IMG" ] || { echo "no such image: $IMG"; exit 2; }

PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
no()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
warn() { WARN=$((WARN+1)); printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
sec()  { printf '\n\033[1m── %s\033[0m\n' "$1"; }

echo "NetBridge image audit"
echo "====================="
echo "  image  $(basename "$IMG")  ($(du -h "$IMG" | cut -f1))"

# ---------------------------------------------------------------- mount
# macOS cannot mount ext4, so the root filesystem is read with debugfs if available and
# otherwise reported as unreadable rather than silently skipped — a skipped check that prints
# nothing is how a missing fix ships.
ATTACH=$(hdiutil attach -readonly -imagekey diskimage-class=CRawDiskImage -nomount "$IMG" 2>/dev/null)
DISK=$(echo "$ATTACH" | head -1 | awk '{print $1}')
[ -n "$DISK" ] || { echo "could not attach image"; exit 2; }
trap 'hdiutil detach "$DISK" -force >/dev/null 2>&1' EXIT
BOOTDEV=$(echo "$ATTACH" | grep -i -m1 'Windows_FAT_32\|DOS_FAT_32' | awk '{print $1}')
MNT=$(mktemp -d)
[ -n "$BOOTDEV" ] && diskutil mount readOnly -mountPoint "$MNT" "$BOOTDEV" >/dev/null 2>&1 || {
  rmdir "$MNT" 2>/dev/null
  echo "could not mount the boot partition read-only"; exit 2;
}
trap 'diskutil unmount force "$MNT" >/dev/null 2>&1; hdiutil detach "$DISK" -force >/dev/null 2>&1; rmdir "$MNT" 2>/dev/null' EXIT

DEBUGFS=$(command -v debugfs || echo /opt/homebrew/opt/e2fsprogs/sbin/debugfs)
have_root=0; [ -x "$DEBUGFS" ] && have_root=1

# This is an A/B image, so there are THREE Linux partitions: rootA, rootB and /data. Picking
# the first one blindly would audit whichever happened to be listed first — which on a fresh
# build is the one that is NOT booted. Identify the root by looking for a file only a root
# filesystem has, and prefer the earliest such partition (slot A, the one that boots first).
ROOTDEV=""
for p in $(echo "$ATTACH" | grep -i 'Linux' | awk '{print $1}'); do
  if "$DEBUGFS" -R "stat /usr/local/bin/bridge-agent.py" "$p" 2>/dev/null | grep -q Inode; then
    ROOTDEV="$p"; break
  fi
done
[ -n "$ROOTDEV" ] || ROOTDEV=$(echo "$ATTACH" | grep -i -m1 'Linux' | awk '{print $1}')

# The third Linux partition is /data, and it is where the fleet config is SEEDED at build
# time. /etc/default/bridge-agent in the rootfs is deliberately a zero-byte bind TARGET — a
# file bind cannot mount onto a path that does not exist. Reading only the rootfs copy makes a
# correctly-provisioned image look unprovisioned, which is exactly the false alarm this
# comment exists to prevent.
DATADEV=""
for p in $(echo "$ATTACH" | grep -i 'Linux' | awk '{print $1}'); do
  [ "$p" = "$ROOTDEV" ] && continue
  if "$DEBUGFS" -R "stat /config/bridge-agent" "$p" 2>/dev/null | grep -q Inode; then
    DATADEV="$p"; break
  fi
done

# ---- ONE pass over the root filesystem. Everything below is answered from these files.
# debugfs's `ls` has no recursive flag (an earlier version passed `-R` and silently read
# nothing, which reported "cannot read ext4" on a perfectly good image). So the index is built
# from the directories that actually matter, each listed once.
IDX=$(mktemp); CAT=$(mktemp -d)
if [ $have_root -eq 1 ]; then
  for d in /usr/local/bin /etc/systemd/system /etc/systemd/system/multi-user.target.wants \
           /etc/bridge /etc/default /data; do
    "$DEBUGFS" -R "ls -l $d" "$ROOTDEV" 2>/dev/null | sed "s|^|$d |" >>"$IDX"
  done
  for f in /etc/passwd /etc/shells /etc/rc.local /boot/cmdline.txt /etc/fstab \
           /home/pi/uvc-raw-setup.sh /usr/local/bin/bridge-agent.py \
           /usr/local/bin/bridge-web.py /usr/local/bin/bridge-read.py \
           /usr/local/bin/bridge-pitch.py /usr/local/bin/bridge-golden.py \
           /usr/local/bin/bridge-jitter.py /usr/local/bin/jitter-sentry.sh \
           /usr/local/bin/bridge-run.sh /etc/os-release \
           /usr/local/bin/bridge-firstboot.sh /usr/local/bin/bridge-wifi-portal.sh \
           /usr/local/bin/bridge-identity.sh \
           /usr/local/bin/bridge-gadget-setup.sh /usr/local/bin/bridge-supervisor.sh \
           /usr/local/bin/bridge-feeder-audio.sh /usr/local/bin/bridge-return-audio.sh \
           /usr/local/bin/bridge-uvcd.sh /usr/local/bin/bridge-watchdog.sh \
           /usr/local/bin/bridge-update.sh /usr/local/bin/bridge-ab \
           /usr/local/bin/bridge-pin /usr/local/bin/bridge-derive-pass \
           /usr/local/bin/bridge-status.sh /usr/local/bin/bridge-diagnose.sh \
           /usr/local/bin/bridge-crackle-sentry.sh /usr/local/bin/flight-recorder.sh \
           /usr/local/bin/flight-recorder.py /usr/local/bin/bridge-feeder-net.sh \
           /usr/local/bin/bridge-video-receiver.py /usr/local/bin/render-idle-frame.py /usr/local/bin/bridge-deploy-script.sh \
           /etc/systemd/journald.conf.d/no-kmsg.conf \
           /usr/local/bin/wifi-guardian.sh /usr/lib/os-release /etc/default/bridge-agent \
           /etc/netbridge/control-url /etc/bridge/control-url \
           /usr/local/bin/bridge-overrides.sh /usr/local/bin/bridge-ssh.sh \
           /usr/local/bin/bridge-cmd-run.sh /etc/netbridge/updatable.conf \
           /etc/netbridge/owner_ssh_authorized_keys /etc/netbridge/ota-pubkey.pem \
           /etc/netbridge/script-pubkey.pem /etc/netbridge/script-signature-v2 \
           /usr/local/bin/bridge-verify-update.py; do
    "$DEBUGFS" -R "cat $f" "$ROOTDEV" >"$CAT/$(basename "$f")" 2>/dev/null
  done
  for u in bridge-agent bridge-web bridge-media bridge-pitch bridge-crackle-sentry bridge-identity \
           bridge-jitter-sentry netbridge-gadget bridge-ssh bridge-overrides bridge-ab-healthcheck \
           netbridge-diagnostics-ssh bridge-pin-gate bridge-pin-sessions; do
    "$DEBUGFS" -R "cat /etc/systemd/system/${u}.service" "$ROOTDEV" >"$CAT/${u}.service" 2>/dev/null
  done
  # Enablement lives in the .wants symlink farm — listed ONCE, not once per unit.
  "$DEBUGFS" -R "ls /etc/systemd/system/multi-user.target.wants" "$ROOTDEV" \
    >"$CAT/.wants" 2>/dev/null
  "$DEBUGFS" -R "ls /etc/systemd/system/timers.target.wants" "$ROOTDEV" \
    >"$CAT/.timers" 2>/dev/null
  "$DEBUGFS" -R "ls /etc/systemd/system/sysinit.target.wants" "$ROOTDEV" \
    >"$CAT/.sysinit" 2>/dev/null
  # The PIN's media gate needs the nft tool (package nftables).
  "$DEBUGFS" -R "stat /usr/sbin/nft" "$ROOTDEV" 2>/dev/null | grep -q Inode && echo yes >"$CAT/.nft"
fi
has() { [ -s "$CAT/$1" ]; }
# Code lines only, for EVERY check. A comment that names a pattern must neither trip a "must not
# contain" check nor satisfy a "must contain" one. Image 2.2.0 exposed three such checks
# (2026-09-25): fleet-brain failed on the comment saying it had been removed, and the setup-AP
# password and PIN count-first checks passed on comments alone. No -q on the second grep: under
# pipefail an early exit can SIGPIPE the first grep and turn a real match into "no match".
grepf() { grep -vE '^[[:space:]]*#' "$CAT/$1" 2>/dev/null | grep -E "$2" >/dev/null; }

# ---------------------------------------------------------------- boot
sec "It will boot at all"
for f in kernel8.img bcm2711-rpi-4-b.dtb cmdline.txt config.txt start4.elf fixup4.dat; do
  [ -f "$MNT/$f" ] && ok "boot/$f present" || no "boot/$f MISSING — will not boot"
done
# The CI runner mounts the Pi boot partition at /boot, not /boot/firmware. Five images were
# lost to writing config into the wrong one, so check the file the bootloader actually reads.
grep -q "root=" "$MNT/cmdline.txt" 2>/dev/null && ok "cmdline.txt names a root device" \
  || no "cmdline.txt has no root= — the classic wrong-mountpoint failure"
grep -q "dwc2" "$MNT/config.txt" 2>/dev/null && ok "dwc2 overlay enabled (USB gadget mode)" \
  || no "no dwc2 overlay — it cannot be a USB device"
grep -q "modules-load=dwc2" "$MNT/cmdline.txt" 2>/dev/null && ok "dwc2 loads at boot" \
  || warn "dwc2 not in modules-load — relies on the overlay alone"
INITRD=$(awk '/^[[:space:]]*initramfs[[:space:]]/{value=$2} END {print value}' "$MNT/config.txt")
[ "$INITRD" = initramfs612-overlay ] && [ -s "$MNT/$INITRD" ] \
  && ok "overlay-capable initramfs selected" || no "A/B image does not select initramfs612-overlay"
# The LAST gpu_mem line wins. The feeder decodes in software (hardware path measured worse live on
# 2026-09-22), so the minimum is right; anything else is an unexplained change.
GPU=$(grep -E '^gpu_mem=' "$MNT/config.txt" 2>/dev/null | tail -1 | cut -d= -f2)
[ "${GPU:-0}" = 16 ] && ok "gpu_mem=16 (software video decoder; RAM left to the system)" \
  || no "gpu_mem=${GPU:-unset} — expected 16"
grep -q "ReadKMsg=no" "$CAT/no-kmsg.conf" 2>/dev/null \
  && ok "journald does not ingest kernel messages (the -61 storm cost 30-37% of a core)" \
  || no "journald ingests kernel messages — the USB -61 storm costs a third of a core"

sec "The root filesystem is readable"
if [ $have_root -eq 1 ] && [ -s "$IDX" ]; then
  ok "ext4 read via debugfs ($(wc -l <"$IDX" | tr -d ' ') entries indexed)"
else
  no "cannot read ext4 — install e2fsprogs (brew install e2fsprogs); NOT auditing the OS"
  echo; echo "  $PASS passed, $FAIL failed, $WARN warnings"; exit 1
fi

# ---------------------------------------------------------------- lifecycle
# The sections below follow what actually happens to a card, in order: power on, join Wi-Fi,
# enrol, bring up the USB gadget, start media, let a presenter go live. A fault anywhere in
# that chain looks identical from the outside ("it doesn't work"), so each stage is checked
# for the specific thing that makes IT fail.
sec "Stage 1 — first boot and provisioning"
has bridge-firstboot.sh && ok "firstboot script present (expands the filesystem, seeds /data)" \
  || no "no firstboot script — a fresh card will not provision itself"
grep -q "bridge-firstboot" "$CAT/.wants" 2>/dev/null && ok "firstboot runs on the first boot" \
  || no "firstboot not enabled — provisioning will never happen"
has bridge-wifi-portal.sh && ok "Wi-Fi setup portal present (the only sanctioned way to join)" \
  || no "no setup portal — a bridge at a new venue could not be joined to Wi-Fi"
# The portal takes its key from bridge-derive-pass, which enforces the value; in the portal itself
# 'bridge2626' is only a comment, so grepping the portal passed on the comment alone (2026-09-25).
grepf bridge-derive-pass '^FIXED=bridge2626$' && grepf bridge-derive-pass '"\$FIXED"' \
  && grepf bridge-wifi-portal.sh '^[[:space:]]*/usr/local/bin/bridge-derive-pass[[:space:]]*(;|$)|\$\(/usr/local/bin/bridge-derive-pass' \
  && ok "setup-AP password is the fixed 'bridge2626' (the agreed policy for every bridge)" \
  || warn "setup-AP password is not the fixed policy value — check before shipping a card"
has wifi-guardian.sh && ok "wifi-guardian present (rejoins when the venue drops it)" \
  || warn "no wifi-guardian — a Wi-Fi blip would leave the bridge offline"

sec "Stage 2 — enrolment into the fleet"
if [ -n "$DATADEV" ]; then
  SEED=$("$DEBUGFS" -R "cat /config/bridge-agent" "$DATADEV" 2>/dev/null)
  URL=$(echo "$SEED" | grep '^CONTROL_URL=' | cut -d= -f2-)
  if [ -n "$URL" ]; then ok "fleet config seeded on /data — enrols at $URL"
  else no "/data/config/bridge-agent has no CONTROL_URL — a fresh card would never enrol"; fi
  echo "$SEED" | grep -q '^BOOTSTRAP_TOKEN=.\+' \
    && ok "bootstrap token present (needed for first enrolment; rotatable server-side)" \
    || no "no bootstrap token — enrolment would be rejected"
else
  no "no /data partition with a seeded config — a fresh card would never enrol"
fi
# The rootfs copy MUST exist and MUST be empty: it is the bind target, nothing more.
if [ -n "$(cat "$CAT/bridge-agent" 2>/dev/null)" ]; then
  warn "the rootfs copy of /etc/default/bridge-agent is non-empty — credentials in the rootfs"
else
  ok "rootfs /etc/default/bridge-agent is an empty bind target (no credentials in the rootfs)"
fi
# The overlay breaks file-binds, so the URL must be written directly rather than mounted in.
grepf bridge-agent.py 'CONTROL_URL|control-url' && ok "agent reads the control URL" \
  || no "agent has no control URL — it can never enrol"
has bridge-ab && ok "A/B slot manager present (bridge-ab)" || warn "no A/B manager"

sec "Stage 3 — the USB gadget comes up"
has bridge-gadget-setup.sh && ok "gadget setup script present" || no "no gadget setup script"
has uvc-raw-setup.sh && ok "UVC/UAC2 descriptor script present" || no "no descriptor script"
grepf uvc-raw-setup.sh '^PRODUCT="Logi"$' && ok "USB product label is Logi" || no "USB product label is not Logi"
grepf uvc-raw-setup.sh 'configfs|usb_gadget' && ok "builds the gadget through configfs" \
  || no "gadget is not built through configfs — it will not enumerate"
has bridge-uvcd.sh && ok "UVC daemon present (serves video to the meeting laptop)" \
  || no "no UVC daemon — the room would see no camera"
grep -q "gadget-clean-detach" "$CAT/.wants" 2>/dev/null \
  && ok "clean-detach on shutdown (stops configfs wedging systemd)" \
  || warn "no clean detach — an unclean stop can wedge the gadget"

sec "Stage 4 — media starts"
has bridge-supervisor.sh && ok "supervisor present (restarts a dead pipeline)" \
  || no "no supervisor — a crashed pipeline would stay dead"
has bridge-feeder-audio.sh && ok "audio feeder present (presenter voice -> room)" \
  || no "no audio feeder — the room could not hear the presenter"
has bridge-return-audio.sh && ok "return audio present (room -> presenter)" \
  || no "no return audio — the presenter could not hear the room"
for u in bridge-feeder-audio bridge-return-audio bridge-uvcd bridge-supervisor bridge-gadget; do
  grep -q "${u}.service" "$CAT/.wants" 2>/dev/null || no "$u not enabled — media will not start"
done
ok "media units checked for enablement"

sec "Stage 5 — a presenter goes live"
has bridge-pin && ok "PIN tool present (set / rotate / lockout)" || no "no PIN tool"
has bridge-derive-pass && ok "password derivation present" || warn "no password derivation"
# NOT 'pin|lock'. That matched 28 times in bridge-web.py, FIFTEEN of them the word
# "clock" -- PIN enforcement could have been deleted outright and this check would still
# have passed. A false PASS in the tool that gates flashing is the worst kind there is.
# Anchor on the actual state object the PIN gate publishes.
# 2026-09-25: the owner went live WITHOUT a PIN. The old check above passed on a bridge where the
# PIN was optional end to end - it only proved a state object existed. These check the parts that
# make the PIN REQUIRED: the ticket gate on go-live, the network gate on the media ports, relock.
grepf bridge-pin 'PROTOCOL = 2' && grepf bridge-pin 'pbkdf2_hmac' \
  && ok "PIN tool is the 2026-09-25 rewrite (salted PBKDF2, one-session tickets)" \
  || no "PIN tool is the OLD one: unlock never relocks, no PIN = open"
grepf bridge-pin 'udp dport \{ %\(v\)d, %\(a\)d \} counter name \\"refused_in\\" drop' \
  && ok "media gate drops video/voice from anyone but the session's presenter" \
  || no "no media gate - anyone on the mesh can stream into the room without the PIN"
grepf bridge-pin '"bridge", *"restart"|bridge restart' && no "PIN tool restarts media (the reboot trigger)" \
  || ok "PIN tool never restarts media"
grepf bridge-web.py 'not self\._session_ok\(caller, body\)' && grepf bridge-web.py '"/api/end-session"' \
  && ok "go-live (set-peer, return-tune) requires the session ticket; Stop ends the session" \
  || no "go-live does not check the PIN session - the PIN can be skipped"
grepf bridge-web.py 'input=\(secret or ""\)' && ok "PIN and ticket reach bridge-pin on stdin (never argv/journal)" \
  || no "PIN passed on a command line - sudo logs it to the journal"
grepf bridge-agent.py 'STDIN = \{"set-pin"' && ! grepf bridge-agent.py '"sudo", "bridge-pin"' \
  && ok "fleet set-pin sends the PIN on stdin; the fleet cannot open a session" \
  || no "agent passes the PIN in argv or can still unlock remotely"
[ -s "$CAT/.nft" ] && ok "nft present (package nftables)" || no "no nft - the media gate cannot be armed"
# The 2026-09-25 audit findings, each of which the previous code got wrong.
# Where the CODE lines sit inside cmd_unlock: the try is written, a failed write refuses, and only
# then is the PIN verified. (This used to grep for the comment "COUNT FIRST", which proves nothing.)
count_first() {
  awk '/^def cmd_unlock\(/ {f = 1; next}
       f && /^def / {f = 0}
       !f || /^[[:space:]]*#/ {next}
       /write_atomic\(TRIES,/ && !w {w = NR}
       /except OSError/ && w && !e {e = NR}
       /^[[:space:]]+out\(\{"ok": False/ && e && !o {o = NR}
       /if not verify\(pin, stored\)/ && !v {v = NR}
       END {exit !(w && e && o && v && w < e && e < o && o < v)}' "$CAT/bridge-pin" 2>/dev/null
}
count_first && ok "a wrong PIN is counted before it is checked (full storage cannot mean unlimited guesses)" \
  || no "PIN tries counted after the check - a full /etc/bridge allows unlimited guesses"
grepf bridge-web.py 'ip\.is_loopback' && ! grepf bridge-web.py 'LOOPBACK = \("127\.", "::1"' \
  && ok "callers are recognised by parsing the address (no ::1:2:3:4 posing as loopback)" \
  || no "the loopback/mesh check is a text prefix - ::1:2:3:4 passes as the bridge itself"
grepf bridge-web.py 'wants_ticket' && ok "the ticket is given only to apps that ask for protocol 2 (old apps display replies)" \
  || no "old apps are handed the session ticket and show it on screen"
grepf bridge-web.py "the return destination must be the caller's own mesh address" \
  && ok "set-peer sends the room's audio only to the caller itself" || no "a ticket holder can send the room's audio to any address"
! grepf bridge-agent.py '"set-peer": +lambda' && ok "the fleet cannot repoint a room's audio (no set-peer in the agent)" \
  || no "the agent still runs set-peer from the fleet - no PIN needed to redirect a room's audio"
grepf bridge-read.py 'etc/bridge\|etc-bridge\)/pin' && ok "read-file refuses the PIN hash, tries, lockout and session" \
  || no "read-file can fetch the PIN hash (cracks offline in minutes)"
grep -q 'bridge-pin-gate.service' "$CAT/.sysinit" 2>/dev/null \
  && ok "media gate closed at every boot (bridge-pin-gate, sysinit)" || no "bridge-pin-gate not enabled - media open after boot"
grep -q 'bridge-pin-sessions.service' "$CAT/.wants" 2>/dev/null \
  && ok "session watcher enabled (relock after 10 min without video / 12 h)" || no "bridge-pin-sessions not enabled - sessions never relock"
grep -q 'nftables.service' "$CAT/.sysinit" "$CAT/.wants" 2>/dev/null \
  && no "Debian nftables.service enabled - its 'flush ruleset' would wipe the gate" \
  || ok "distro nftables loader not enabled"
# 'config' appears everywhere; anchor on the call that actually publishes it.
grepf bridge-web.py 'd\["config"\] *= *golden_state\(\)' && ok "status exposes the live config (Golden Profile)" \
  || warn "config not exposed — drift could not be detected"
# 'pcm' alone is far too loose; anchor on the assignment.
grepf bridge-web.py 'd\["pcm"\] *= *_return_pcm\(\)' && ok "status exposes the capture ring (PCM pointers)" \
  || no "PCM pointers not exposed — the pitch controller and snapshots go blind"

sec "Stage 6 — it keeps running"
has bridge-watchdog.sh && ok "watchdog present" || warn "no watchdog script"
# 2026-09-24: the camera stream must fit in ONE isochronous packet per microframe. At 640x360 it
# needed 2048-byte "high-bandwidth" packets, where the Pi 4's USB controller missed slots and the
# meeting laptop saw choppy video in every app. Every place that sets the frame size must agree.
UW=$(grep -oE 'create_frame \$FUNCTION [0-9]+ [0-9]+ uncompressed' "$CAT/uvc-raw-setup.sh" 2>/dev/null | awk '{print $3"x"$4}')
UMP=$(grep -oE 'echo [0-9]+ > functions/\$FUNCTION/streaming_maxpacket' "$CAT/uvc-raw-setup.sh" 2>/dev/null | awk '{print $2}')
LCAP=$(grep -oE 'YUYV:[0-9]+x[0-9]+@' "$CAT/bridge-gadget-setup.sh" 2>/dev/null | head -1 | sed 's/YUYV://; s/@//')
FCAP=$(grep -oE 'format=YUY2,width=[0-9]+,height=[0-9]+' "$CAT/bridge-video-receiver.py" 2>/dev/null | sed -E 's/.*width=([0-9]+),height=([0-9]+)/\1x\2/')
ICAP=$(grep -oE '^W, H = [0-9]+, [0-9]+' "$CAT/render-idle-frame.py" 2>/dev/null | sed -E 's/W, H = ([0-9]+), ([0-9]+)/\1x\2/')
[ "$UW" = 424x240 ] && ok "USB camera advertises 424x240 (fits one packet per microframe at 30 fps)" \
  || no "USB camera advertises ${UW:-?} — expected 424x240"
[ -n "$UMP" ] && [ "$UMP" -le 1024 ] && ok "streaming_maxpacket $UMP (no high-bandwidth isochronous)" \
  || no "streaming_maxpacket ${UMP:-?} — high-bandwidth mode is where the USB controller missed slots"
if [ -n "$UW" ] && [ "$UW" = "$LCAP" ] && [ "$UW" = "$FCAP" ] && [ "$UW" = "$ICAP" ]; then
  ok "frame size agrees everywhere: descriptor, loopback, feeder, idle frame ($UW)"
else
  no "frame size disagrees: descriptor=${UW:-?} loopback=${LCAP:-?} feeder=${FCAP:-?} idle=${ICAP:-?}"
fi
# Frame RATE must agree too: a sender/gadget mismatch is judder by construction (the Mac camera
# captures 30; sending 20 kept 2 frames of every 3, 33/67 ms apart). 30 fps since 2026-09-24.
UI=$(awk '/dwFrameInterval$/{getline; print; exit}' "$CAT/uvc-raw-setup.sh" 2>/dev/null | tr -d ' ')
UFPS=$([ -n "$UI" ] && [ "$UI" -gt 0 ] 2>/dev/null && echo $(( (10000000 + UI/2) / UI )))
LFPS=$(grep -oE 'YUYV:[0-9]+x[0-9]+@[0-9]+/1' "$CAT/bridge-gadget-setup.sh" 2>/dev/null | head -1 | sed -E 's/.*@([0-9]+)\/1/\1/')
# USB owns output pacing; the receiver must publish only freshly decoded frames.
# A videorate-generated frame would incorrectly extend the freeze deadline.
if [ "${UFPS:-x}" = 30 ] && [ "$UFPS" = "$LFPS" ] \
   && grepf bridge-video-receiver.py 'appsink name=frames.*sync=false.*max-buffers=1.*drop=true' \
   && ! grepf bridge-video-receiver.py '![[:space:]]*videorate|v4l2sink'; then
  ok "USB output is 30 fps; receiver publishes latest decoded frames without synthetic freshness"
else
  no "USB pacing or decoded-frame delivery mismatch: descriptor=${UFPS:-?} loopback=${LFPS:-?}"
fi
# 2026-09-24: no automatic media restarts on a fresh card. The WAN profile (owner's standing choice)
# is seeded on /data, and jitter-sentry never switches back to lan - every switch restarts the
# whole media stack, which on an under-powered bridge was followed by a reboot 4/4 times.
if [ -n "$DATADEV" ]; then
  NETSEED=$("$DEBUGFS" -R "cat /config/bridge-net" "$DATADEV" 2>/dev/null)
  echo "$NETSEED" | grep -qx 'NET_AUDIO_LATENCY=300' && echo "$NETSEED" | grep -qx 'NET_VIDEO_LATENCY=300' \
    && ok "WAN profile seeded on /data (voice 300 ms as validated; video capped to 100 ms by the feeder)" \
    || no "/data/config/bridge-net is not the WAN profile — a fresh card starts on unset defaults"
else
  no "no /data partition to check the seeded media profile on"
fi
grep -vE '^[[:space:]]*#' "$CAT/jitter-sentry.sh" 2>/dev/null | grep -q 'switch_profile lan' \
  && no "jitter-sentry can still switch the bridge to lan (owner's rule: never)" \
  || ok "jitter-sentry never switches to lan (and so never restarts media for it)"
# 2026-09-24 working configuration: the camera service counts USB misses (the referee for video
# tuning), and signed deploys flush to the card before restarting (a reset there left 0-byte files).
grepf bridge-uvcd.sh 'netbridge-usb-video.txt' && grepf bridge-uvcd.sh 'USB_IRQ_CPUS="2"' \
  && ok "camera service: USB-miss counter built in, USB interrupt on CPU 2" \
  || no "camera service lacks the USB-miss counter or the CPU 2 interrupt setting"
awk '/^install -m 0644 +"\$STAGE\/\$NAME.sig"/{i=NR} /^sync/{if(i&&!s)s=NR} /"\$OVR" policy "\$NAME"/{if(i&&!r)r=NR} END{exit !(i && s && r && s>i && s<r)}' "$CAT/bridge-deploy-script.sh" 2>/dev/null \
  && ok "signed deploys flush to the card before restarting the service" \
  || no "deploy script restarts before flushing — a reset can leave 0-byte overrides"
# 2026-09-22 CPU sweep: each of these was measured costing CPU on a live bridge.
has flight-recorder.py && grepf flight-recorder.sh 'exec /usr/bin/python3 /usr/local/bin/flight-recorder.py' \
  && ok "flight recorder is the no-launch Python loop" \
  || no "flight recorder is the old shell loop (~10 launches/s, whole-system sync every second)"
grepf bridge-uvcd.sh '^  > >\(exec grep --line-buffered -v' \
  && ok "camera EAGAIN flood filtered on stdout (where libuvcgadget prints it)" \
  || no "camera EAGAIN flood not filtered on stdout — journald floods again"
grepf bridge-feeder-net.sh '^exec /usr/bin/python3 /usr/local/bin/bridge-video-receiver.py' && grepf bridge-video-receiver.py 'avdec_h264' && ! grep -vE '^[[:space:]]*#' "$CAT/bridge-video-receiver.py" | grep -q 'v4l2h264dec' \
  && ok "video feeder decodes in software (the hardware path measured worse live)" \
  || no "video feeder uses the hardware decoder path (74% vs 51% of a core live)"
grepf bridge-video-receiver.py 'latency=%d' && grepf bridge-feeder-net.sh '"\$VLAT" -gt 100\ ' \
  && ok "video jitter buffer capped at 100 ms (profile stays WAN)" \
  || no "video jitter buffer follows the 300 ms WAN profile — ~200 ms extra lag"
grepf bridge-agent.py 'LOCAL_STATUS = "http://127.0.0.1:8080/api/status"' \
  && ok "fleet agent reuses the running status page (no cold rebuild every 15 s)" \
  || no "fleet agent rebuilds the full status in a fresh process every 15 s"
grepf bridge-web.py '^def _cached\(' && ok "status page caches its answer" \
  || no "status page launches ~20 programs per request"
# Judged only when the timer list could be read at all (it must at least hold the agent's
# timer) - an unreadable list must not pass as "not enabled".
if ! grep -q "bridge-agent.timer" "$CAT/.timers" 2>/dev/null; then
  warn "could not list enabled timers — idle-frame timer state not checked"
elif grep -q "bridge-idle-frame.timer" "$CAT/.timers"; then
  no "idle-frame timer enabled — re-renders a constant black frame every 20 s"
else
  ok "idle-frame timer not enabled (the frame is constant)"
fi
has flight-recorder.sh && ok "flight recorder present (the persistent black box)" \
  || no "no flight recorder — brownouts would be invisible again"
grep -q "flight-recorder" "$CAT/.wants" 2>/dev/null && ok "flight recorder is enabled" \
  || no "flight recorder installed but not enabled — power faults go unrecorded"
grepf bridge-web.py 'flight' \
  && ok "power reading comes from the flight recorder, not vcgencmd's error text" \
  || no "power readout may be reporting an error string as a value (the 2026-08 bug)"
has bridge-update.sh && ok "OTA updater present" || warn "no OTA updater"
grep -q "bridge-ab-healthcheck" "$CAT/.wants" 2>/dev/null \
  && ok "A/B healthcheck enabled (auto-rollback if a slot does not come back)" \
  || no "no A/B healthcheck — a bad update could not roll itself back"

# ---------------------------------------------------------------- the fixes
sec "The audio fixes are actually in this image"
grepf uvc-raw-setup.sh '48000,44100,32000 *> *[A-Za-z0-9/._]*c_srate' \
  && ok "multi-rate descriptor 48k/44.1k/32k (M3, verified on hardware)" \
  || no "c_srate is not the three-rate string — multi-rate is NOT in this image"
for f in bridge-feeder-audio.sh bridge-return-audio.sh; do
  grepf "$f" 'S16LE' && ok "$f pins S16LE caps (the silent-audio bug)" \
    || no "$f does not pin S16LE — the silent-audio bug can return"
grepf uvc-raw-setup.sh 'tune_sync|gadget-tuning' \
  && ok "reads /data/gadget-tuning.conf (sync mode + req_number changeable without a rebuild)" \
  || no "tuning file not read — c_sync/req_number experiments need a full image cycle again"
  if grep -qE '\! *\!' "$CAT/$f" 2>/dev/null; then
    no "$f has an empty '! !' element — the unset-variable bug is back"
  else ok "$f has no empty pipeline element from an unset variable"; fi
  if grep -qE 'conceal(ment)?=(true|1)' "$CAT/$f" 2>/dev/null; then
    no "$f re-enables opusdec concealment — the 2026-08-03 jitter regression"
  else ok "$f does not re-enable concealment (the proven jitter cause)"; fi
done
grepf jitter-sentry.sh 'JITTER_SENTRY_LIVE_RUNG' \
  && ok "sentry's live rung is opt-in (it cannot raise the buffer on its own)" \
  || warn "sentry may act on live audio unprompted"


sec "The fixes from 2026-08-24"
# Each of these was a fault seen on real hardware the same day. An image that ships without
# one of them looks identical to one that has it, until the fault happens again.
grepf bridge-web.py 'def _stream_live' \
  && ok "stream liveness is a counter DELTA, not 'the service is running'" \
  || no "liveness is still the old proxy — the fleet will show an idle bridge as LIVE"
if grepf bridge-web.py '"video": *svc\.get\("bridge-feeder-net"\) == "active" and attached'; then
  no "the old always-true liveness test is still present"
else
  ok "the always-true liveness test is gone"
fi
grepf bridge-web.py '_stream_live\("return"' \
  && ok "return audio liveness comes from the capture pointer moving" \
  || no "return liveness not measured from hw_ptr"
# The field that identifies an image must survive first boot. It did not: firstboot wrote
# "dev" over the baked version on every card, which is why a running bridge had to be
# identified by fingerprinting an unrelated bug in its status output.
# THE WRITE MOVED, SO CHECK WHERE IT LIVES NOW.
#
# This used to require a `if [ -n "${BRIDGE_VERSION:-}" ]` guard inside firstboot. Firstboot no
# longer writes the version AT ALL -- the whole job moved to bridge-identity.sh, which runs at
# EVERY boot rather than once, because on a read-only root the hostname and version have to be
# re-applied each time. Demanding the old guard failed a strictly better design, and would have
# blocked a good image with DO NOT FLASH.
#
# The bug the original check existed to catch is still caught, in two places: firstboot must not
# write the version unconditionally (checked below), and the surviving write must only fall back
# to "dev" when there is genuinely no seed to use.
if grepf bridge-firstboot.sh '^[^#]*>/etc/bridge/version'; then
  no "firstboot writes the version again — that job belongs to bridge-identity.sh, which runs
        every boot; firstboot runs once and cannot maintain it on a read-only root"
else
  ok "firstboot no longer writes the version (bridge-identity.sh owns it)"
fi
if grepf bridge-identity.sh 'elif \[ ! -s /etc/bridge/version \]'; then
  ok "the version only falls back to 'dev' when no seed exists at all"
else
  no "bridge-identity.sh may clobber a good version with 'dev' — images become unidentifiable"
fi
if grepf bridge-firstboot.sh 'echo "\$\{BRIDGE_VERSION:-dev\}" >/etc/bridge/version'; then
  no "the unconditional 'dev' write is still there"
else
  ok "no unconditional 'dev' write remains"
fi

sec "It can say when it is still settling after a flash"
# Flashing rewrites the whole disk, /data included, so the /data/.expanded guard goes with it
# and the filesystem expansion runs AGAIN on that boot - resize2fs plus ssh-keygen -A against
# a real-time media pipeline. An operator who goes live immediately hears bursts that stop by
# themselves, with every metric reading healthy throughout.
grepf bridge-web.py 'def settling' \
  && ok "the bridge can report that post-flash work is still running" \
  || no "no settling signal — bursts after a flash look identical to a fault"
grepf bridge-web.py 'STILL SETTLING' \
  && ok "it says so in /api/checks, the line the app actually displays" \
  || no "settling is not surfaced where an operator would see it"
grepf bridge-web.py 'FIRSTBOOT_UNITS' \
  && ok "watches the one-shot units that only run after a flash" \
  || no "does not know which units to watch"

sec "A flashed card knows its own name and version"
has bridge-identity.sh && ok "bridge-identity.sh installed (runs every boot)" \
  || no "no identity script — the hostname would never be applied"
grep -q "bridge-identity.service" "$CAT/.wants" 2>/dev/null \
  && ok "bridge-identity is ENABLED at boot" \
  || no "identity script present but not enabled — it will never run"
grepf bridge-identity.sh 'hostname "\$NEWHOST"' \
  && ok "sets the kernel hostname directly (the only thing that works on a read-only root)" \
  || no "relies on writing /etc/hostname, which this image cannot do"
grepf bridge-firstboot.sh 'NEWHOST="netbridge-' \
  && ok "hostname is derived from the pairing code" || no "no hostname rename"
# Both used to sit BELOW the provision-conf early exit, which fires on every one of these
# images, so neither ever ran: every card stayed "raspberrypi" and reported version "dev".
# IDENTITY MUST NOT DEPEND ON A PROVISION CONF.
#
# The original failure: firstboot exits early when no provision conf is present -- which is
# EVERY normal card -- so any identity work sitting after that line never ran. This used to
# require the hostname AND version writes to appear before that exit inside firstboot.
#
# The version write has since left firstboot entirely, so requiring it there failed a good
# image. What actually has to be true is unchanged in substance: identity work must happen on
# a card that has no provision conf. Check both halves of how that is now guaranteed.
if [ "$(python3 -c "
t=open('$CAT/bridge-firstboot.sh').read()
ex=t.find('no provision conf found'); ho=t.find('hostnamectl set-hostname')
print('ok' if (ex>0 and 0<ho<ex) else 'bad')" 2>/dev/null)" = "ok" ]; then
  ok "firstboot sets the hostname BEFORE its provision-conf early exit"
else
  no "firstboot's hostname work sits after the early exit — it will never run on a normal card"
fi
if grepf bridge-identity.service 'ConditionPathExists=/data/provision' ; then
  no "bridge-identity only runs when a provision conf exists — it must run on every card"
else
  ok "bridge-identity runs regardless of any provision conf"
fi
if [ -n "$DATADEV" ]; then
  V=$("$DEBUGFS" -R "cat /etc-bridge/version" "$DATADEV" 2>/dev/null | tr -d '\r\n ')
  if [ -n "$V" ]; then ok "version seeded on /data where it is actually readable: $V"
  else no "/data/etc-bridge/version is empty — the bind shadows the rootfs copy, so it reads 'dev'"; fi
fi

sec "The new remote-read capability"
has bridge-read.py && ok "bridge-read.py installed" || no "bridge-read.py MISSING"
grepf bridge-read.py 'os\.path\.realpath\(r\) for r in _ROOTS' \
  && ok "roots are resolved before comparison (bug #1 fixed)" \
  || no "roots compared unresolved — a symlinked root refuses its own files"
grepf bridge-read.py 'os\.path\.realpath\(p\) for p in \(' \
  && ok "deny-list is resolved too (bug #3 — agent.token was reachable)" \
  || no "deny-list unresolved — the credential files are NOT protected"
# NOT 'token\|secret\|password'. grepf uses grep -E, where \| is a LITERAL pipe, not
# alternation -- so this searched for the string "token|secret|password" and passed only
# because bridge-read.py happens to contain exactly that inside its redaction regex. It
# was verifying a coincidence, and a harmless reordering of that regex would have failed
# an image whose redaction still worked perfectly. Anchor on the redaction being APPLIED.
grepf bridge-read.py 'text, redacted = redact\(text\)' \
  && ok "secret redaction present" || no "no redaction — tokens would be returned verbatim"
grepf bridge-agent.py 'read-file' && ok "agent allows the read-file command" \
  || no "agent does not know read-file — the panel button will be refused on the device"

sec "Obsolete services cannot return after first-boot presets"
for unit in bridge-feeder.service bridge-testpattern.service bridge-idle-frame.timer bridge-crackle-sentry.service bridge-pitch.service ssh.service ssh.socket; do
  "$DEBUGFS" -R "stat /etc/systemd/system/$unit" "$ROOTDEV" 2>/dev/null | grep -q 'Fast link dest: "/dev/null"' \
    && ok "$unit permanently masked" || no "$unit missing production mask"
done

sec "Services that must be enabled, not merely installed"
# bridge-crackle-sentry shipped in every image for weeks and was never enabled, so the metric
# it feeds could only ever report 'false'. Installed != enabled.
#
# The expected list is DERIVED from the repo's own unit files, not typed here. An earlier
# version hardcoded four names and three of them were wrong — it reported netbridge-gadget and
# bridge-media as missing when the real units are bridge-gadget and bridge-supervisor, and
# called bridge-agent "not enabled" when it is started by bridge-agent.timer and correctly has
# no WantedBy at all. A check that invents its own expectations tests nothing but my memory.
REPO_UNITS="$(cd "$(dirname "$0")/.." && ls pi/systemd/*.service 2>/dev/null)"
if [ -z "$REPO_UNITS" ]; then
  warn "cannot see pi/systemd — skipping the derived service checks"
else
  # Units that carry WantedBy but must NOT be enabled in a production image: they are test
  # modes. bridge-testpattern declares Conflicts=bridge-feeder-net, so enabling it would
  # actively break real video.
  EXPECT_DISABLED="bridge-feeder bridge-testpattern bridge-soak bridge-crackle-sentry bridge-pitch"
  MISSING=0; NOTEN=0; NCHK=0
  for f in $REPO_UNITS; do
    u=$(basename "$f" .service)
    want=$(grep -h '^WantedBy' "$f" | cut -d= -f2 | tr -d ' ')
    NCHK=$((NCHK+1))
    if ! "$DEBUGFS" -R "stat /etc/systemd/system/${u}.service" "$ROOTDEV" 2>/dev/null \
         | grep -q Inode; then
      no "$u.service is in the repo but NOT in the image"; MISSING=$((MISSING+1)); continue
    fi
    # No WantedBy means it is triggered by a timer or another unit, not by a target — so its
    # absence from the .wants farm is correct, not a fault.
    case " $EXPECT_DISABLED " in *" $u "*) continue ;; esac
    case "$want" in
      *multi-user*) grep -q "${u}.service" "$CAT/.wants" 2>/dev/null \
                      || { no "$u is installed but NOT enabled — it will never run"
                           NOTEN=$((NOTEN+1)); } ;;
    esac
  done
  [ "$MISSING" -eq 0 ] && ok "all $NCHK repo units are present in the image"
  [ "$NOTEN" -eq 0 ] && ok "every unit with WantedBy=multi-user.target is enabled at boot"
  for u in $EXPECT_DISABLED; do
    grep -q "${u}.service" "$CAT/.wants" 2>/dev/null \
      && no "$u is a TEST unit and must not be enabled in a shipping image" \
      || ok "$u correctly left disabled (test mode)"
  done
  # The one that is deliberately timer-driven — verify the timer, since the service alone
  # would sit there forever.
  "$DEBUGFS" -R "stat /etc/systemd/system/bridge-agent.timer" "$ROOTDEV" 2>/dev/null \
    | grep -q Inode && ok "bridge-agent.timer present (this is what starts the agent)" \
    || no "bridge-agent.timer MISSING — the bridge will never poll for commands"
  # These two are installed but must NOT be enabled. That flipped on 2026-08-14: neither had
  # ever been shown to help, and bridge-pitch does nothing at all while the gadget is in
  # adaptive mode. Fewer services during a live session means fewer variables in an audio
  # fault we still cannot explain. Installed-and-available, off by default.
  for u in bridge-crackle-sentry bridge-pitch; do
    "$DEBUGFS" -R "stat /etc/systemd/system/${u}.service" "$ROOTDEV" 2>/dev/null | grep -q Inode \
      && ok "$u is installed (available to enable deliberately)" \
      || no "$u missing entirely — it should ship, just not run"
    grep -q "${u}.service" "$CAT/.wants" 2>/dev/null \
      && no "$u is ENABLED — it must be off by default" \
      || ok "$u correctly NOT enabled"
  done
fi

sec "Safety rails"
grepf bridge-agent.py 'ALLOWED' && ok "agent re-validates commands against its own allow-list" \
  || no "agent has no allow-list — the device trusts the backend blindly"
# Match the MECHANISM, not a word. The first pattern here was /minisign|signify|verify/ and
# none of those appear — the loader spells it "verifies" and uses openssl. A check that greps
# for a word the code never uses fails on a perfectly good image.
has script-signature-v2 && has bridge-verify-update.py \
  && ok "signed script identity/replay enforcement is included" \
  || no "missing mandatory signed-script context enforcement"
grepf bridge-run.sh 'openssl.*(dgst|verify)|script-pubkey' \
  && ok "script overrides are signature-checked (openssl against script-pubkey.pem)" \
  || no "unsigned overrides would execute"
grepf bridge-agent.py 'quarantine' && ok "auto-rollback quarantine present" \
  || warn "no quarantine — a bad override cannot self-revert"

sec "Remote maintenance: signed updates, rollback, owner SSH, OTA (2026-09-24)"
_owner_shell=$(awk -F: '$1=="pi" {print $7}' "$CAT/passwd" 2>/dev/null)
[ "$_owner_shell" = /bin/bash ] && grep -qxF "$_owner_shell" "$CAT/shells" \
  && ok "owner SSH account has a valid login shell" || no "owner SSH account cannot open its login shell"

# What a signed update may replace is the read-only catalog. The safety net itself must never be
# in it: the loader, the installer, the rollback guard, the command runner, the A/B switch and
# the security/provisioning tools change only with a whole new image.
if has updatable.conf; then
  ok "update catalog /etc/netbridge/updatable.conf is on the read-only root"
  _bad=""
  for _n in bridge-run.sh bridge-deploy-script.sh bridge-overrides.sh bridge-cmd-run.sh bridge-ab \
            bridge-pin bridge-derive-pass bridge-identity.sh bridge-firstboot.sh; do
    awk -v n="$_n" '!/^[[:space:]]*#/ && $1==n {f=1} END {exit !f}' "$CAT/updatable.conf" && _bad="$_bad $_n"
  done
  [ -z "$_bad" ] && ok "the safety net is NOT remotely replaceable (loader, installer, rollback, A/B, PIN)" \
                 || no "catalog lets a remote update replace:$_bad"
  _miss=""
  for _t in $(awk '!/^[[:space:]]*#/ && NF>=5 && ($3=="bind" || $3=="loader") {print $2}' "$CAT/updatable.conf"); do
    case "$_t" in
      /usr/local/bin/*) grep -q " $(basename "$_t")\$" "$IDX" 2>/dev/null || grep -qE "[[:space:]]$(basename "$_t")[[:space:]]*$" "$IDX" || _miss="$_miss $(basename "$_t")" ;;
      /home/pi/uvc-raw-setup.sh) has uvc-raw-setup.sh || _miss="$_miss uvc-raw-setup.sh" ;;
    esac
  done
  [ -z "$_miss" ] && ok "every updatable file exists in the image (a bind needs a built-in to cover)" \
                  || no "catalog names files the image does not have:$_miss"
else
  no "no update catalog — nothing beyond the four media scripts can be updated without a reflash"
fi
grepf bridge-overrides.sh 'openssl dgst -sha256 -verify' && grepf bridge-overrides.sh 'script-pubkey.pem' \
  && ok "every override is signature-checked at every boot (bridge-overrides.sh)" \
  || no "bridge-overrides.sh does not verify signatures"
grepf bridge-overrides.sh 'BOOT_LIMIT=3' && grepf bridge-overrides.sh 'safe-mode' && grepf bridge-run.sh 'safe-mode' \
  && ok "safe mode: 3 unhealthy boots in a row -> every built-in file, no overrides" \
  || no "no safe mode — a bad boot-time update could keep the bridge down"
grepf bridge-overrides.sh 'LIFELINE_QUIET_S' && grepf bridge-overrides.sh 'quarantine' \
  && ok "lifeline guard + crash-loop rollback (agent / Wi-Fi updates revert if the fleet goes quiet)" \
  || no "no automatic rollback for bound updates"
grepf bridge-overrides.sh 'laptop_attached' && grepf updatable.conf 'bridge-uvcd\.sh +[^ ]+ +loader +camera' \
  && ok "camera updates wait for the laptop to be unplugged (restart with it attached reboots the Pi)" \
  || no "a camera update could restart the camera under an attached laptop"
grep -q 'bridge-overrides.service' "$CAT/.sysinit" 2>/dev/null \
  && ok "bridge-overrides runs at every boot (sysinit)" || no "bridge-overrides is not enabled"
grep -q 'bridge-overrides-health.timer' "$CAT/.timers" 2>/dev/null \
  && ok "rollback/pending health timer enabled" || no "bridge-overrides-health.timer not enabled"
# Owner SSH: tailnet address only, owner key only, no root, no passwords, no forwarding.
if has bridge-ssh.sh; then
  _ssh_bad=""
  for _p in 'PermitRootLogin no' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
            'AuthenticationMethods publickey' 'AllowTcpForwarding no' 'PermitTunnel no' \
            'AllowUsers \$USER_ALLOWED@100.64.0.0/10' 'ListenAddress \$1'; do
    grepf bridge-ssh.sh "$_p" || _ssh_bad="$_ssh_bad [$_p]"
  done
  grepf bridge-ssh.sh '12\[0-7\]' || _ssh_bad="$_ssh_bad [tailnet-range check]"
  [ -z "$_ssh_bad" ] && ok "owner SSH: tailnet address only, key only, no root/passwords/forwarding" \
                     || no "owner SSH config is missing:$_ssh_bad"
else
  no "bridge-ssh.sh missing — no remote shell for fixes"
fi
grep -q 'bridge-ssh.service' "$CAT/.wants" 2>/dev/null && ok "bridge-ssh enabled" || no "bridge-ssh not enabled"
grep -qE '^ssh\.service|[[:space:]]ssh\.service' "$CAT/.wants" 2>/dev/null \
  && no "the stock sshd is enabled (would listen on every interface)" || ok "stock sshd not enabled"
has netbridge-diagnostics-ssh.service && no "collaborator's root diagnostic SSH unit is installed" \
  || ok "no root diagnostic SSH unit"
grep -qE '^restrict,pty ssh-(ed25519|rsa|ecdsa)' "$CAT/owner_ssh_authorized_keys" 2>/dev/null \
  && ok "owner SSH key present, restricted (shell only, no forwarding)" \
  || no "owner SSH key missing or unrestricted"
# Whole-OS updates: verified with a key on the read-only root, judged by a health check a bridge
# can actually pass, and run outside the agent so they are not killed half-way.
has ota-pubkey.pem && ok "OTA public key on the read-only root" || no "OTA public key only on /data (writable)"
grepf bridge-ab 'fleet-brain' && no "A/B health check still waits for fleet-brain (every trial would roll back)" \
  || { grepf bridge-ab 'bridge-agent/last-ok' && ok "A/B trial is committed only after a fleet heartbeat" \
       || no "A/B health check does not require fleet contact"; }
grepf bridge-update.sh '\-\-fleet' && grepf bridge-update.sh 'meeting laptop is attached' \
  && ok "OTA refuses live sessions and schedules its trial boot outside the agent" \
  || no "OTA can start under a live meeting or be killed with the agent"
grepf bridge-agent.py 'DETACHED' && grepf bridge-agent.py 'collect_results' && has bridge-cmd-run.sh \
  && ok "slow fleet commands run in the background and still report (fixes D2)" \
  || no "slow fleet commands still run inside the agent (killed at its timeout, result lost)"
grepf bridge-agent.py 'mark_ok' && ok "agent records every accepted heartbeat (used by rollback + A/B)" \
  || no "agent does not record fleet contact"

sec "Every panel button is accepted by THIS image's agent"
# The real end-to-end gate check. A command must pass three independent allow-lists: the panel
# offers it, the backend forwards it, and the agent on the device accepts it. Adding a button
# without the third produces a button that fails only on real hardware — which is exactly how
# read-file would have shipped broken. Compare the panel's list against the agent IN THE IMAGE.
PANEL="$(cd "$(dirname "$0")/.." && python3 - <<'PY' 2>/dev/null
import re, pathlib
try:
    s = pathlib.Path("control-plane/panel-dist/index.html").read_text()
    blk = re.search(r"const ACTIONS = \[(.*?)\n\];", s, re.S).group(1)
    for v, _ in re.findall(r'\["([^"]*)",\s*"([^"]*)"\]', blk):
        if v and not v.startswith("#"):
            print(v.split(":")[0])
except Exception:
    pass
PY
)"
if [ -n "$PANEL" ] && [ -s "$CAT/bridge-agent.py" ]; then
  BAD=""
  # Some panel entries are handled entirely by the backend and never reach a device —
  # mesh-key is POST /admin/devices/{id}/mesh-key. Expecting the agent to know it is wrong.
  BACKEND_ONLY="mesh-key"
  for c in $(echo "$PANEL" | sort -u); do
    case " $BACKEND_ONLY " in *" $c "*) continue ;; esac
    grep -q "\"$c\"\|'$c'" "$CAT/bridge-agent.py" || BAD="$BAD $c"
  done
  if [ -z "$BAD" ]; then
    ok "all $(echo "$PANEL" | sort -u | wc -l | tr -d ' ') distinct panel commands are in the agent's allow-list"
  else
    no "the agent in this image would REFUSE:$BAD"
  fi
else
  warn "could not compare the panel and the agent"
fi

sec "Nothing secret was baked in"
for pat in 'tskey-[a-z0-9]' 'AKIA[0-9A-Z]{16}' 'ghp_[A-Za-z0-9]{20}'; do
  if grep -rlE "$pat" "$CAT" >/dev/null 2>&1; then no "a live credential matching /$pat/ is in the image"
  else ok "no credential matching /$pat/"; fi
done
if [ -s "$CAT/agent.token" ]; then no "an agent token is baked in — every card would share it"
else ok "no pre-baked agent token (each bridge enrols its own)"; fi

sec "What is actually in this image"
echo "  OS         $(awk -F= '/^PRETTY_NAME/{gsub(/"/,"",$2); print $2}' "$CAT/os-release" 2>/dev/null)"
echo "  kernel     $(ls "$MNT" | grep -c '^kernel' ) kernel image(s), $(ls "$MNT"/*.dtb 2>/dev/null | wc -l | tr -d ' ') device trees"
echo "  bridge scripts installed:"
awk '$1=="/usr/local/bin"' "$IDX" | tr ' ' '\n' | grep -E '^(bridge|uvc|jitter|wifi|flight|render)' \
  | sort -u | awk '{printf "    %-28s", $1; if (NR%3==0) printf "\n"} END {print ""}'
echo "  services enabled at boot: $(tr ' ' '\n' <"$CAT/.wants" | grep -c '\.service')"
printf '  fleet commands the agent accepts: %s\n' \
  "$(grep -oE '\"[a-z][a-z-]{3,}\"' "$CAT/bridge-agent.py" | sort -u | wc -l | tr -d ' ')"

echo
# ---------------------------------------------------------------- baked secrets
sec "Secrets that are deliberately baked in are still locked down"
# /etc/default/bridge-agent carries the FLEET-WIDE bootstrap token. It is meant to be there --
# a shipped bridge cannot self-enrol without it -- but at 0644 any local account on the device
# could read it, and anyone who obtained a card could enrol rogue devices against the fleet.
#
# It shipped at 0644 because two places write this file: bridge-firstboot.sh uses umask 077 +
# chmod 600, while the CI image build baked it at 0644, and the CI path is the one that
# actually ships. The builder is fixed; this checks the ARTIFACT, which is the only claim
# that counts.
agentline=$(grep -E '^/etc/default .*bridge-agent$' "$IDX" 2>/dev/null | head -1)
if [ -z "$agentline" ]; then
  warn "no /etc/default/bridge-agent in the image — a fresh card cannot self-enrol"
else
  # debugfs `ls -l` prints: <inode>  <mode>  <uid>  <gid>  <size> <date> <name>
  amode=$(printf '%s' "$agentline" | awk '{print $3}')
  case "$amode" in
    *100600|*600) ok "/etc/default/bridge-agent is 0600 (bootstrap token not world-readable)" ;;
    "")           warn "could not read the mode of /etc/default/bridge-agent" ;;
    *)            no "/etc/default/bridge-agent is mode $amode — the fleet bootstrap token is
        readable by any local account, and by anyone holding this card" ;;
  esac
fi

printf '\033[1m  %d passed, %d failed, %d warnings\033[0m\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
  echo "  → DO NOT FLASH: file audit failed"
elif [ "$WARN" -ne 0 ]; then
  echo "  → File checks completed with warnings; review them before spare-card testing"
else
  echo "  → File checks passed; boot and physical USB/media acceptance still required"
fi
rm -rf "$IDX" "$CAT"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
