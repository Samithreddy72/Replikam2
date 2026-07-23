#!/bin/bash
# One-shot diagnostic: writes WiFi / portal / radio state to the FAT boot partition
# (/boot/firmware/BRIDGE-DIAG.txt) so a card that won't raise its setup AP can be debugged
# by pulling it and reading the file on any computer — no SSH, console or network needed.
OUT=/boot/firmware/BRIDGE-DIAG.txt
{
  echo "=== NetBridge boot diagnostic (uptime $(cat /proc/uptime 2>/dev/null | cut -d. -f1)s) ==="
  echo "--- version ---"; cat /etc/netbridge-image-version 2>/dev/null; uname -a
  echo; echo "--- rfkill (blocked? soft/hard) ---"; rfkill list 2>&1
  echo; echo "--- iw reg (regulatory domain) ---"; iw reg get 2>&1 | grep -E "country|global" 
  echo; echo "--- interfaces ---"; ip -br link 2>&1; ip -br addr 2>&1
  echo; echo "--- iw dev (does wlan0 exist?) ---"; iw dev 2>&1
  echo; echo "--- nmcli ---"; nmcli -t dev 2>&1; echo "radio: $(nmcli radio 2>&1)"; echo "conn: $(nmcli -t -f CONNECTIVITY general 2>&1)"
  echo; echo "--- wifi-connect present? ---"; ls -la /usr/local/sbin/wifi-connect 2>&1; ls /usr/local/share/wifi-connect/ui/index.html 2>&1
  echo; echo "--- setup AP pass generated? ---"; test -s /etc/bridge/setup-wifi-pass && echo present || echo MISSING
  echo; echo "--- kernel (want 6.12.93 pinned) ---"; uname -r 2>&1; echo "config kernel line: $(grep -E '^kernel=' /boot/firmware/config.txt 2>&1)"
  echo; echo "--- root + /data mounts ---"; findmnt -no SOURCE,FSTYPE,OPTIONS / 2>&1; findmnt -no SOURCE,FSTYPE,OPTIONS /data 2>&1
  echo "data.mount: $(systemctl is-active data.mount 2>&1)"; systemctl status data.mount --no-pager 2>&1 | head -8
  echo "etc-bridge writable? $(touch /etc/bridge/.w 2>&1 && echo yes && rm -f /etc/bridge/.w || echo NO-READONLY)"
  echo; echo "--- /data expanded? ---"; df -h /data 2>&1; echo; lsblk -o NAME,SIZE,LABEL 2>&1
  echo; echo "--- brcm/wlan/regulatory dmesg ---"; dmesg 2>&1 | grep -iE "brcmfmac|wlan|cfg80211|regulatory|rfkill|firmware.*brcm" | tail -40
  echo; echo "--- bridge-wifi-unblock ---"; systemctl status bridge-wifi-unblock.service --no-pager 2>&1 | head -12
  echo; echo "--- bridge-wifi-portal ---"; systemctl status bridge-wifi-portal.service --no-pager 2>&1 | head -20
  echo; echo "--- portal journal (last 60) ---"; journalctl -u bridge-wifi-portal.service --no-pager -n 60 2>&1
  echo; echo "=== end ==="
} > "$OUT" 2>&1 || true
sync 2>/dev/null || true
