#!/bin/bash
# preflight-check.sh — runs at the END of ci-build-image.sh, inside the built rootfs.
# Asserts the image actually CONTAINS everything its own code needs, so a missing package
# FAILS THE BUILD instead of publishing a signed image that is dead in the field.
#
# Every check below maps to a real defect that passed all of CI (build+sign+publish) and was
# caught only by flashing hardware: no setup portal (wifi-connect), /data never expanded
# (growpart/parted), WiFi radio off (rfkill). This is the guard that ends that class.
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
FAIL=0
need_bin(){ command -v "$1" >/dev/null 2>&1 || { echo "  PREFLIGHT FAIL: missing binary '$1'  ($2)"; FAIL=1; }; }
need_file(){ [ -e "$1" ] || { echo "  PREFLIGHT FAIL: missing file '$1'  ($2)"; FAIL=1; }; }
need_enabled(){ systemctl is-enabled "$1" >/dev/null 2>&1 || { echo "  PREFLIGHT FAIL: service '$1' not enabled  ($2)"; FAIL=1; }; }

echo "== preflight: critical runtime binaries =="
need_bin wifi-connect     "setup portal — without it a card cannot be onboarded"
need_bin dnsmasq          "captive-portal DHCP/DNS"
need_bin rfkill           "unblock the WiFi radio — without it no setup AP appears"
need_bin iw               "set WiFi regulatory domain"
need_bin growpart         "expand /data on first boot"
need_bin parted           "expand /data (fallback) + partprobe"
need_bin partprobe        "re-read partition table after growpart"
need_bin resize2fs        "grow the /data filesystem"
need_bin gst-launch-1.0   "media pipeline"
need_bin v4l2loopback-ctl "virtual camera device"
need_bin nmcli            "WiFi management"
need_bin tailscale        "mesh join at claim"
need_bin python3          "agent + web + scripts"

echo "== preflight: critical files =="
need_file /usr/local/bin/bridge-verify-update.py "signed update identity and replay guard"
need_file /etc/netbridge/script-signature-v2 "mandatory signed update policy"
need_file /usr/local/sbin/wifi-connect                "portal binary"
need_file /usr/local/share/wifi-connect/ui/index.html "portal UI assets"
need_file /usr/local/bin/uvc-gadget                   "smooth-video gadget"
need_file "${BOOTDIR:-/boot/firmware}/kernel612.img"        "pinned kernel"
need_file "${BOOTDIR:-/boot/firmware}/initramfs612-overlay" "overlay initramfs (read-only root)"

echo "== preflight: critical services enabled =="
for u in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio \
         bridge-web bridge-wifi-portal bridge-wifi-unblock wifi-guardian bridge-firstboot \
         bridge-regen-hostkeys; do
  need_enabled "$u" "core service"
done

echo "== preflight: enabled bridge units point at binaries that exist (auto-check) =="
# Auto-catches a NEW unit whose ExecStart names a binary the image forgot to ship.
# WARN-only (absolute paths only) so an odd ExecStart can't spuriously break the build.
for unit in /etc/systemd/system/bridge-*.service; do
  [ -e "$unit" ] || continue
  systemctl is-enabled "$(basename "$unit")" >/dev/null 2>&1 || continue
  bin=$(grep -m1 '^ExecStart=' "$unit" | sed 's/^ExecStart=//; s/^[-@+]*//' | awk '{print $1}')
  case "$bin" in /*) [ -e "$bin" ] || echo "  PREFLIGHT WARN: $(basename "$unit") ExecStart '$bin' not present";; esac
done

if [ "$FAIL" = 0 ]; then echo "== preflight: PASS =="; else
  echo "== preflight: FAILED — refusing to publish an image missing its own runtime deps =="; exit 1; fi

# ---- read-only-root config trap -------------------------------------------------
# Four separate bugs this week were the same shape: a script writes a config file to a
# path on the READ-ONLY root, the write fails silently, and the feature is simply dead
# (set-peer, fleet claiming, dnsmasq leases, resolv.conf). Fail the build if a shipped
# script writes to a config path that is not backed by /data.
echo "== preflight: read-only config writes =="
_writable="/data /etc/bridge /var/lib/tailscale /etc/NetworkManager/system-connections
/var/log/journal /home/pi/diagnostics /tmp /run /var/tmp /var/lib/misc /var/cache
/var/spool /var/lib/systemd /var/lib/dhcp /etc/default/bridge-agent
/etc/default/bridge-net /etc/default/bridge-return-audio
/home/pi/flight.txt /home/pi/soak.log /etc/hostname"
_bad=0
for _f in "${ROOT:-}"/usr/local/bin/*; do
  [ -f "$_f" ] || continue
  grep -ohE '(>>?|tee -?a?)[[:space:]]*"?(/etc|/var|/usr|/home|/boot)[a-zA-Z0-9._/-]*' "$_f" 2>/dev/null \
   | sed -E 's/^(>>?|tee -?a?)[[:space:]]*//; s/^"//' | sort -u | while read -r _p; do
      _ok=0
      for _w in $_writable; do case "$_p" in "$_w"*) _ok=1;; esac; done
      [ "$_ok" = 0 ] && echo "  READ-ONLY WRITE: $(basename "$_f") -> $_p"
    done
done | tee /tmp/_ro_writes.txt
if [ -s /tmp/_ro_writes.txt ]; then
  echo "FATAL: script(s) write config to the read-only root; bind the path to /data first"
  exit 1
fi
echo "  no read-only config writes"
