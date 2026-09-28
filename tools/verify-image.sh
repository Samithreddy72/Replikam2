#!/usr/bin/env bash
# File-by-file + feature verifier for a NetBridge image, run on the Mac before flashing: the second
# opinion next to tools/image-audit.sh.
#
# WHY IT IS IN THE REPO
# ---------------------
# Until 2026-09-25 this lived in a session scratchpad under /tmp (verify-image-v3.sh, calibrated
# on 8434651 and 52a161b), and a reboot deleted it. A check that can vanish is not part of the
# release process.
#
# WHAT IT ADDS TO THE AUDIT
# -------------------------
#   1. every bridge script, unit, config and camera binary in the image is BYTE-IDENTICAL to git
#   2. every unit that must start is enabled where it must be (multi-user / sysinit / timers) and
#      the ones that must stay off are off, matched by EXACT unit name (a substring match counted
#      bridge-ssh.service as the stock "ssh.service")
#   3. the boot config the firmware reads, and the kernel it really boots (kernel= in config.txt)
#   4. what the code needs is in the OS, and every script parses
#   5. the video/audio chain is what Everything-good shipped (424x240@30, WaysToGo features)
#   6. the PIN media gate can work on THAT kernel (nf_tables pieces) and nothing reopens it
#   7. things that must NOT be in the image
#   8. (with a baseline image) every system package identical except what this build adds, and the
#      same boot chain (kernel + initramfs) as the baseline
# Greps read code lines only: a comment that names a pattern proves nothing (see image-audit.sh).
#
#   bash tools/verify-image.sh <image.img> <commit> [baseline.img]
set -uo pipefail
IMG="${1:?usage: verify-image.sh <image.img> <commit> [baseline.img]}"
COMMIT="${2:?usage: verify-image.sh <image.img> <commit> [baseline.img]}"
BASE="${3:-}"
PINNED_KERNEL="${PINNED_KERNEL:-6.12.93+rpt-rpi-v8}"   # the kernel the gadget work was proven on
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEBUGFS=$(command -v debugfs || echo /opt/homebrew/opt/e2fsprogs/sbin/debugfs)
[ -x "$DEBUGFS" ] || { echo "debugfs not found (brew install e2fsprogs)"; exit 2; }
git -C "$REPO" cat-file -e "$COMMIT^{commit}" 2>/dev/null || { echo "unknown commit: $COMMIT"; exit 2; }
P=0; F=0; W=0
ok(){ P=$((P+1)); echo "  PASS  $1"; }
no(){ F=$((F+1)); echo "  FAIL  $1"; }
note(){ W=$((W+1)); echo "  NOTE  $1"; }

T=$(mktemp -d); DISKS=""; MOUNTS=""
cleanup(){
  for m in $MOUNTS; do diskutil unmount force "$m" >/dev/null 2>&1; rmdir "$m" 2>/dev/null; done
  for d in $DISKS; do hdiutil detach "$d" -force >/dev/null 2>&1; done
  rm -rf "$T"
}
trap cleanup EXIT
attach(){   # attach IMAGE -> A_BOOT, A_ROOT (slot A: the first root with bridge-web.py), A_MNT (boot, read-only)
  local out p
  out=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount "$1" 2>/dev/null) || return 1
  DISKS="$DISKS $(echo "$out" | head -1 | awk '{print $1}')"
  A_BOOT=$(echo "$out" | awk '/Windows_FAT|DOS_FAT|FAT_32/{print $1; exit}')
  A_ROOT=""
  for p in $(echo "$out" | awk '/Linux/{print $1}'); do
    "$DEBUGFS" -R "stat /usr/local/bin/bridge-web.py" "$p" 2>/dev/null | grep -q Inode && { A_ROOT=$p; break; }
  done
  A_MNT=$(mktemp -d); MOUNTS="$MOUNTS $A_MNT"
  diskutil mount readOnly -mountPoint "$A_MNT" "$A_BOOT" >/dev/null 2>&1
  [ -n "$A_ROOT" ]
}
attach "$IMG" || { echo "could not attach $IMG (or no root filesystem with bridge-web.py)"; exit 2; }
BOOT=$A_BOOT; ROOT=$A_ROOT; BMNT=$A_MNT
echo "image: $(basename "$IMG")  boot: $BOOT  root: $ROOT  commit: $COMMIT"

cat_img(){ "$DEBUGFS" -R "cat $1" "$ROOT" 2>/dev/null; }
exists(){ "$DEBUGFS" -R "stat $1" "$ROOT" 2>/dev/null | grep -q Inode; }
names(){ "$DEBUGFS" -R "ls -p $1" "$ROOT" 2>/dev/null | awk -F/ 'NF>5 && $6!="." && $6!=".." {print $6}'; }
g(){ grep -vE '^[[:space:]]*#' "$T/$1" 2>/dev/null | grep -E -- "$2" >/dev/null; }   # code lines only
kver(){     # kernel version string inside a kernel image file (raw or gzip)
  local k="$1"
  [ -f "$k" ] || return 1
  if [ "$(head -c2 "$k" | xxd -p)" = "1f8b" ]; then gzip -dc < "$k"; else cat "$k"; fi \
    | LC_ALL=C strings -n 12 | grep -m1 -oE 'Linux version [^ ]+' | awk '{print $3}'
}

echo; echo "--- 1. every bridge script, unit, config and camera binary byte-identical to git $COMMIT ---"
cmp_git(){  # cmp_git GITPATH IMAGEPATH -> 0 same, 1 differs/missing
  git -C "$REPO" show "$COMMIT:$1" > "$T/git" 2>/dev/null || return 1
  cat_img "$2" > "$T/img"; cmp -s "$T/git" "$T/img"
}
bad=0; n=0
for f in $(git -C "$REPO" ls-tree --name-only "$COMMIT" pi/scripts/ | grep -v __pycache__); do
  [ "$(git -C "$REPO" cat-file -t "$COMMIT:$f" 2>/dev/null)" = blob ] || continue
  b=$(basename "$f"); dst=/usr/local/bin/$b; [ "$b" = uvc-raw-setup.sh ] && dst=/home/pi/$b
  n=$((n+1)); cmp_git "$f" "$dst" || { bad=$((bad+1)); echo "        differs/missing: $dst"; }
done
[ $bad -eq 0 ] && ok "$n scripts identical to $COMMIT" || no "$bad of $n scripts differ from $COMMIT"
bad=0; n=0
for f in $(git -C "$REPO" ls-tree --name-only "$COMMIT" pi/systemd/ | grep -E '\.(service|timer)$'); do
  n=$((n+1)); cmp_git "$f" "/etc/systemd/system/$(basename "$f")" || { bad=$((bad+1)); echo "        differs/missing: $(basename "$f")"; }
done
[ $bad -eq 0 ] && ok "$n systemd units identical to git" || no "$bad of $n units differ"
bad=0; n=0
while read -r src dst; do
  git -C "$REPO" cat-file -e "$COMMIT:$src" 2>/dev/null || continue
  n=$((n+1)); cmp_git "$src" "$dst" || { bad=$((bad+1)); echo "        differs/missing: $dst"; }
done <<'MAP'
pi/configs/script-pubkey.pem /etc/netbridge/script-pubkey.pem
pi/configs/ota-pubkey.pem /etc/netbridge/ota-pubkey.pem
pi/configs/updatable.conf /etc/netbridge/updatable.conf
pi/configs/owner_ssh_authorized_keys /etc/netbridge/owner_ssh_authorized_keys
pi/configs/v4l2loopback.conf /etc/modprobe.d/v4l2loopback.conf
pi/configs/size-cap.conf /etc/systemd/journald.conf.d/size-cap.conf
pi/configs/journald-persistent.conf /etc/systemd/journald.conf.d/journald-persistent.conf
pi/configs/no-kmsg.conf /etc/systemd/journald.conf.d/no-kmsg.conf
pi/configs/kit-watchdog.conf /etc/systemd/system.conf.d/99-watchdog.conf
pi/configs/wifi-powersave-off.conf /etc/NetworkManager/conf.d/wifi-powersave-off.conf
pi/configs/no-mac-rand.conf /etc/NetworkManager/conf.d/no-mac-rand.conf
restore/binaries/uvc-gadget /usr/local/bin/uvc-gadget
restore/binaries/libuvcgadget.so.0.4.0 /usr/local/lib/aarch64-linux-gnu/libuvcgadget.so.0.4.0
MAP
[ $bad -eq 0 ] && ok "$n configs, keys and camera binaries identical to git" || no "$bad of $n configs/keys/binaries differ"

owner_shell=$(cat_img /etc/passwd | awk -F: '$1=="pi" {print $7}')
[ "$owner_shell" = /bin/bash ] && cat_img /etc/shells | grep -qxF "$owner_shell" \
  && ok "owner SSH login shell is enabled" || no "owner SSH login shell is missing or disabled"

echo; echo "--- 2. units that start, where they must, and the ones that must not ---"
names /etc/systemd/system/multi-user.target.wants > "$T/wants"
names /etc/systemd/system/timers.target.wants > "$T/timers"
names /etc/systemd/system/sysinit.target.wants > "$T/sysinit"
names /etc/systemd/system/sockets.target.wants > "$T/sockets"
[ -s "$T/wants" ] || no "could not list multi-user.target.wants"
on(){ grep -qxF "$2" "$T/$1"; }
miss=""
for u in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio wifi-guardian \
         bridge-powertrim flight-recorder jitter-sentry bridge-supervisor bridge-web bridge-wifi-portal \
         bridge-idle-frame gadget-clean-detach bridge-identity tailscaled bridge-pin-sessions bridge-ssh; do
  on wants "$u.service" || miss="$miss $u"
done
for t in bridge-agent bridge-watchdog bridge-overrides-health; do on timers "$t.timer" || miss="$miss $t.timer"; done
for u in bridge-pin-gate bridge-overrides; do on sysinit "$u.service" || miss="$miss $u(sysinit)"; done
[ -z "$miss" ] && ok "all 23 required units enabled (18 services, 3 timers, 2 at sysinit)" || no "NOT enabled:$miss"
bad=""
for u in bridge-crackle-sentry.service bridge-pitch.service bridge-testpattern.service bridge-idle-frame.timer \
         nftables.service ssh.service ssh.socket netbridge-diagnostics-ssh.service; do
  for d in wants timers sysinit sockets; do on $d "$u" && bad="$bad $u"; done
done
[ -z "$bad" ] && ok "stay off: crackle-sentry, pitch, testpattern, idle-frame timer, distro nftables loader, stock sshd, root diagnostic SSH" \
  || no "enabled but must stay off:$bad"

echo; echo "--- 3. boot config the firmware reads, and the kernel it really boots ---"
CFG="$BMNT/config.txt"; KV=""
if [ -f "$CFG" ]; then
  last(){ grep -E "^$1=" "$CFG" | tail -1 | cut -d= -f2; }
  [ "$(last gpu_mem)" = 16 ] && ok "gpu_mem=16 (software decoder; RAM to the system)" || no "gpu_mem=$(last gpu_mem), expected 16"
  [ "$(last arm_freq)" = 900 ] && ok "arm_freq=900 (unchanged cap)" || no "arm_freq=$(last arm_freq)"
  [ "$(last arm_boost)" = 0 ] && ok "arm_boost=0 (unchanged)" || no "arm_boost=$(last arm_boost)"
  grep -q '^dtoverlay=dwc2,dr_mode=peripheral' "$CFG" && ok "USB gadget mode (dwc2 peripheral)" || no "no dwc2 peripheral overlay"
  grep -q '^dtparam=audio=off' "$CFG" && ok "analog audio off (unchanged)" || no "audio=off missing"
  [ -f "$BMNT/start4cd.elf" ] && [ -f "$BMNT/fixup4cd.dat" ] && ok "cut-down firmware present (used at gpu_mem=16)" || no "start4cd.elf/fixup4cd.dat missing"
  [ "$(grep -Ec '^gpu_mem=' "$CFG")" = 1 ] && ok "exactly one gpu_mem line" || note "$(grep -Ec '^gpu_mem=' "$CFG") gpu_mem lines (last wins)"
  KF=$(last kernel); KF=${KF:-kernel8.img}
  KV=$(kver "$BMNT/$KF")
  [ "$KV" = "$PINNED_KERNEL" ] && ok "boots $KF = Linux $KV (the pinned kernel)" || no "boots $KF = Linux ${KV:-unreadable}, expected $PINNED_KERNEL"
  INITRD=$(grep -E '^initramfs ' "$CFG" | tail -1 | awk '{print $2}')
  if [ -n "$INITRD" ]; then
    [ -s "$BMNT/$INITRD" ] && ok "initramfs $INITRD present" || no "config.txt names initramfs $INITRD but it is missing"
  fi
  exists "/usr/lib/modules/$KV/modules.dep" && ok "modules for $KV are installed (modules.dep present)" || no "no /usr/lib/modules/$KV - the booted kernel has no modules"
else
  no "could not read config.txt from the boot partition"
fi

echo; echo "--- 4. what the code needs is in the OS, and every script parses ---"
exists /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlibav.so && ok "avdec_h264 (gst-libav) present" || no "libgstlibav.so missing"
exists /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstwebrtcdsp.so && ok "webrtcdsp present (voice AGC chain)" || no "webrtcdsp plugin missing"
m=""
for b in /usr/bin/python3 /usr/bin/stdbuf /usr/bin/grep /usr/bin/journalctl /usr/bin/vcgencmd /usr/bin/tailscale /usr/sbin/nft /usr/bin/logger; do
  exists "$b" || m="$m $b"
done
[ -z "$m" ] && ok "python3, stdbuf, grep, journalctl, vcgencmd, tailscale, nft, logger present" || no "missing:$m"
py=0; sh=0; badp=""
for b in $(names /usr/local/bin); do
  case "$b" in *.so*|uvc-gadget) continue ;; esac
  cat_img "/usr/local/bin/$b" > "$T/s"; [ -s "$T/s" ] || continue
  first=$(head -c 100 "$T/s" | head -1)
  case "$first" in
    "#!"*) ;;
    *) continue ;;
  esac
  case "$first" in
    *python*) py=$((py+1)); python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$T/s" 2>/dev/null || badp="$badp $b" ;;
    *bash*|*/sh*) sh=$((sh+1)); bash -n "$T/s" 2>/dev/null || badp="$badp $b" ;;
  esac
done
[ -z "$badp" ] && ok "$py Python and $sh shell scripts in /usr/local/bin parse" || no "do not parse:$badp"

echo; echo "--- 5. the video/audio chain is what Everything-good shipped ---"
cat_img /usr/local/bin/bridge-feeder-net.sh > "$T/fn"
g fn 'avdec_h264' && ! grep -vE '^[[:space:]]*#' "$T/fn" | grep -q v4l2h264dec && ok "video: software decoder only (hardware path measured worse)" || no "video decoder not software-only"
g fn 'latency=\$VLAT' && g fn '"\$VLAT" -gt 100 ' && ok "video: jitter buffer capped at 100 ms (profile stays WAN)" || no "video buffer cap missing"
cat_img /etc/systemd/journald.conf.d/no-kmsg.conf > "$T/kmsg"; g kmsg '^ReadKMsg=no' && ok "journald: kernel messages not ingested (ReadKMsg=no)" || no "journald no-kmsg drop-in missing"
cat_img /usr/local/bin/flight-recorder.py > "$T/frpy"; g frpy '^PULL_WINDOW = 4.0' && ok "flight recorder: 4 s camera window" || no "flight recorder window not 4 s"
cat_img /usr/local/bin/bridge-uvcd.sh > "$T/uvcd"; g uvcd '^  > >\(exec grep --line-buffered -v' && ok "camera: EAGAIN flood filtered on stdout" || no "camera filter missing/wrong stream"
cat_img /usr/local/bin/bridge-web.py > "$T/web"; g web '^def _cached\(' && g web '^def _services_active\(' && ok "status page: cached, one systemctl call" || no "status page caching missing"
cat_img /usr/local/bin/bridge-agent.py > "$T/agent"; g agent 'LOCAL_STATUS = "http://127.0.0.1:8080/api/status"' && g agent 'd.get\("tailscale_ip"\)' && ok "agent: reads the running status page, root fallback" || no "agent change missing"
cat_img /usr/local/bin/flight-recorder.sh > "$T/fr"; g fr 'exec /usr/bin/python3 /usr/local/bin/flight-recorder.py' && ok "flight recorder: Python loop" || no "flight recorder change missing"
cat_img /usr/local/bin/bridge-feeder-audio.sh > "$T/fa"; g fa 'if \[ "\$\{RETURN_AEC:-0\}" = "1" \]' && ok "mic feeder: echo-cancel copy only when AEC on" || no "mic feeder change missing"
cat_img /home/pi/uvc-raw-setup.sh > "$T/uvc"; cat_img /usr/local/bin/bridge-gadget-setup.sh > "$T/gs"; cat_img /usr/local/bin/render-idle-frame.py > "$T/idl"
g uvc 'create_frame \$FUNCTION 424 240 uncompressed u' && ok "UVC descriptor frame 424x240" || no "UVC descriptor frame is not 424x240"
awk '/dwFrameInterval$/{getline; print; exit}' "$T/uvc" | grep -qx 333333 && ok "UVC frame interval 333333 = 30 fps" || no "UVC frame interval is not 30 fps"
g uvc 'echo 1024 > functions/\$FUNCTION/streaming_maxpacket' && ok "streaming_maxpacket 1024 (one packet per microframe)" || no "streaming_maxpacket is not 1024"
g gs '"YUYV:424x240@30/1"' && ok "loopback caps 424x240@30" || no "loopback caps wrong"
g fn 'format=YUY2,width=424,height=240,framerate=30/1' && ok "feeder output 424x240@30" || no "feeder output wrong"
g idl '^W, H = 424, 240' && ok "idle frame 424x240" || no "idle frame size wrong"
g web 'dwFrameInterval' && ok "status page reads fps from the descriptor" || no "status page expected-fps fix missing"
cat_img /usr/local/bin/bridge-return-audio.sh > "$T/ret"; cat_img /usr/local/bin/bridge > "$T/cli"
g uvc 'echo 3 > functions/uac2.usb0/c_chmask' && ok "USB capture is STEREO (c_chmask 3)" || no "stereo c_chmask 3 missing"
g uvc 'echo 3 > functions/uac2.usb0/p_chmask' && ok "USB speaker is STEREO (p_chmask 3)" || no "stereo p_chmask 3 missing"
g uvc '48000,44100,32000' && ok "multi-rate capture 48/44.1/32 kHz" || no "multi-rate missing"
g fa 'gain-control=true' && ! grep -vE '^[[:space:]]*#' "$T/fa" | grep -q 'volume=6' && ok "voice chain: AGC on, no x6 volume stage" || no "voice chain changed"
g ret '/data/config/bridge-return-tune' && ok "return audio reads /data/config tuning" || no "return tuning path missing"
g cli 'TUNE=/data/config/bridge-return-tune' && ok "bridge return-tune writes /data/config" || no "return-tune writer missing"
g web '"/api/return-tune"' && ok "status page GET /api/return-tune" || no "/api/return-tune missing"
g uvcd 'smp_affinity_list' && ok "camera daemon moves the USB IRQ to CPU 2" || no "IRQ affinity missing"
g idl 'bytes\(\(16, 128, 16, 128\)\)' && ok "idle camera frame = plain black" || no "idle frame changed"

echo; echo "--- 6. the PIN media gate can work on the booted kernel, and nothing reopens it ---"
NF="/usr/lib/modules/$KV/kernel/net/netfilter"
if [ -n "$KV" ] && "$DEBUGFS" -R "dump $NF/nf_tables.ko.xz $T/nft.ko.xz" "$ROOT" 2>/dev/null && [ -s "$T/nft.ko.xz" ]; then
  xz -dc "$T/nft.ko.xz" 2>/dev/null | LC_ALL=C strings -n 3 > "$T/nft.str"
  grep -q "^vermagic=$KV " "$T/nft.str" && ok "nf_tables.ko built for the booted kernel ($KV)" || no "nf_tables.ko vermagic does not match $KV"
  m=""
  for w in objref counter lookup payload meta; do grep -qx "$w" "$T/nft.str" || m="$m $w"; done
  grep -q '^nft_chain_filter_inet' "$T/nft.str" || m="$m inet-filter-chain"
  grep -qE '^nft_set_(rhash|hash)' "$T/nft.str" || m="$m set-backend"
  [ -z "$m" ] && ok "nf_tables has what the gate uses: inet filter chain, sets, named counters, UDP port match, iifname" \
    || no "nf_tables lacks:$m"
  cat_img "/usr/lib/modules/$KV/modules.dep" | grep -q '/nf_tables\.ko\.xz:' && ok "modules.dep lists nf_tables (it loads on first use)" || no "nf_tables not in modules.dep"
else
  no "no nf_tables module for the booted kernel ${KV:-?}"
fi
cat_img /var/lib/dpkg/status > "$T/status"
ver(){ awk -v p="$1" '/^Package: /{k=$2} /^Status: /{s=$0} /^Version: /{if (k==p && s ~ /install ok installed/) {print $2; exit}}' "$T/status"; }
[ -n "$(ver nftables)" ] && ok "nft userland: nftables $(ver nftables)" || no "nftables package not installed"
fw=""; for p in firewalld ufw iptables-persistent netfilter-persistent; do [ -n "$(ver $p)" ] && fw="$fw $p"; done
[ -z "$fw" ] && ok "no other firewall manager that could flush the gate (firewalld, ufw, *-persistent)" || no "installed firewall managers:$fw"
cat_img /etc/systemd/system/bridge-pin-gate.service > "$T/gate"
g gate '^DefaultDependencies=no' && g gate '^WantedBy=sysinit.target' && g gate 'bridge-pin gate-init' \
  && ok "bridge-pin-gate: early boot (no default deps), wanted by sysinit, runs gate-init" || no "bridge-pin-gate unit changed"
cat_img /etc/systemd/system/bridge-pin-sessions.service > "$T/sess"
g sess 'bridge-pin watch' && g sess '^Restart=always' && ok "session watcher restarts forever (it re-arms a flushed gate)" || no "session watcher unit changed"

echo; echo "--- 7. things that must NOT be in the image ---"
exists /etc/systemd/system/netbridge-diagnostics-ssh.service && no "diagnostic root SSH service present" || ok "no diagnostic root SSH service"
exists /etc/bridge/agent.token && no "agent token baked in" || ok "no per-device agent token"
names /var/lib/tailscale | grep -qx "tailscaled.state" && no "tailnet identity baked in" || ok "no tailnet identity in the image"
exists /opt/replikam2 && no "repository copy inside the image" || ok "no repository copy inside the image"
names /etc/ssh | grep -q '^ssh_host_' && no "SSH host keys baked in (every card would share them)" || ok "no SSH host keys baked in (made per device at first boot)"

if [ -n "$BASE" ]; then
  echo; echo "--- 8. against $(basename "$BASE"): same packages except this build's additions, same boot chain ---"
  if attach "$BASE"; then
    "$DEBUGFS" -R "cat /var/lib/dpkg/status" "$A_ROOT" > "$T/status.base" 2>/dev/null
    out=$(python3 - "$T/status.base" "$T/status" <<'PY'
import re, sys
def parse(path):
    pk, deps = {}, {}
    for block in open(path, encoding="utf-8", errors="replace").read().split("\n\n"):
        f = dict(re.findall(r"^([A-Za-z-]+): (.*)$", block, re.M))
        if f.get("Package") and "install ok installed" in f.get("Status", ""):
            key = (f["Package"], f.get("Architecture", ""))
            pk[key] = f.get("Version", "")
            names = re.split(r"[,|]", f.get("Depends", "") + "," + f.get("Pre-Depends", ""))
            deps[f["Package"]] = {re.sub(r"[\s(:].*$", "", n.strip()) for n in names if n.strip()}
    return pk, deps
old, _ = parse(sys.argv[1]); new, deps = parse(sys.argv[2])
closure, todo = set(), ["nftables"]
while todo:                                   # nftables and everything it pulls in
    p = todo.pop()
    if p not in closure:
        closure.add(p); todo += list(deps.get(p, ()))
added = sorted(k for k in new if k not in old)
removed = sorted(k for k in old if k not in new)
changed = sorted(k for k in new if k in old and new[k] != old[k])
same = len(new) - len(added) - len(changed)
unexpected = [k for k in added if k[0] not in closure]
fmt = lambda ks: ", ".join("%s %s" % (k[0], new.get(k, old.get(k))) for k in ks)
print("  %s  %d packages, %d identical to the baseline" % ("PASS" if not (removed or changed or unexpected) else "FAIL", len(new), same))
print("  %s  added: %s" % ("FAIL" if unexpected else "PASS",
                         (fmt(added) + (" (NOT pulled in by nftables: %s)" % fmt(unexpected) if unexpected else "")) or "nothing"))
print("  %s  removed: %s" % ("FAIL" if removed else "PASS", fmt(removed) or "nothing"))
print("  %s  changed version: %s" % ("FAIL" if changed else "PASS",
      ", ".join("%s %s -> %s" % (k[0], old[k], new[k]) for k in changed) or "nothing"))
PY
)
    echo "$out"
    P=$((P + $(printf '%s\n' "$out" | grep -c '^  PASS'))); F=$((F + $(printf '%s\n' "$out" | grep -c '^  FAIL')))
    [ -n "$out" ] || no "could not compare packages with the baseline"
    KF_=$(grep -E '^kernel=' "$CFG" | tail -1 | cut -d= -f2)
    [ -n "$KF_" ] && { cmp -s "$BMNT/$KF_" "$A_MNT/$KF_" && ok "boot chain: $KF_ byte-identical to the baseline" \
                                                       || no "boot chain: $KF_ differs from the baseline"; }
    # An initramfs is regenerated by every build, so its BYTES always differ (timestamps inside).
    # What matters is what it contains: same files, modes, owners and contents.
    IR_=$(grep -E '^initramfs ' "$CFG" | tail -1 | awk '{print $2}')
    if [ -n "$IR_" ]; then
      out=$(python3 "$REPO/tools/cpio-compare.py" "$A_MNT/$IR_" "$BMNT/$IR_" 2>&1); rc=$?
      [ $rc -eq 0 ] && ok "boot chain: $IR_ holds the same files as the baseline ($out)" \
                    || no "boot chain: $IR_ contents differ from the baseline: $out"
    fi
  else
    no "could not attach the baseline $BASE"
  fi
fi

echo; echo "  TOTAL $P passed, $F failed, $W notes"
[ $F -eq 0 ]
