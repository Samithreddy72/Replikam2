#!/bin/bash
# Per-device identity: the bridge's name, and the version it is running.
#
# WHY THIS IS ITS OWN SCRIPT, RUN ON EVERY BOOT
# ----------------------------------------------
# The root filesystem is read-only ext4 (/dev/mmcblk0p2 / ext4 ro,relatime), so there is no
# file on this device holding the hostname. sethostname(2) is the only thing that works, and
# it does not survive a reboot - so something must set it at every boot.
#
# It used to live inside bridge-firstboot.sh, which happens to run every boot only because
# that script's `systemctl disable` is unreachable while no provisioning file exists. Relying
# on an unreachable line is not a mechanism; the day a provision conf appears, firstboot would
# self-disable and every bridge would quietly revert to "raspberrypi".
#
# WHY THE NAME MATTERS
# --------------------
# Every card ships as "raspberrypi". Two bridges on one venue LAN then both claim
# raspberrypi.local, mDNS resolution becomes a coin flip, and the router's client list shows
# two identical rows. The pairing code is the identifier already printed on the label and
# shown in the fleet, so the device is recognisable by one name everywhere:
# netbridge-2626.local, netbridge-2626 on the tailnet.
set -u
log(){ logger -t bridge-identity "$*"; echo "  $*"; }

PC="$(cat /etc/bridge/pairing-code 2>/dev/null || true)"
if [ -z "${PC:-}" ]; then
  # Derived from the CPU serial exactly as the fleet derives it, so the name a bridge gives
  # itself always matches the code on its label - no stored file to lose or disagree with.
  SER="$(awk -F': *' '/^Serial/{print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
  [ -n "${SER:-}" ] && PC="$(printf '%s' "$SER" | sha256sum | cut -c1-4 | tr 'a-f' 'A-F')"
fi

if [ -n "${PC:-}" ]; then
  NEWHOST="netbridge-${PC}"
  if [ "$(hostname)" != "$NEWHOST" ]; then
    # hostnamectl tries to WRITE /etc/hostname and returns 0 on this image while changing
    # nothing, which is why the old fallback never ran. Never trust its exit code.
    hostnamectl set-hostname "$NEWHOST" >/dev/null 2>&1 || true
    [ "$(hostname)" = "$NEWHOST" ] || hostname "$NEWHOST" 2>/dev/null || true
    # Only if something has made the root writable. Skipped silently otherwise; the kernel
    # name set above is what avahi and mDNS actually answer with.
    [ -w /etc/hostname ] && echo "$NEWHOST" >/etc/hostname 2>/dev/null || true
    [ -w /etc/hosts ] && sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEWHOST/" /etc/hosts 2>/dev/null || true

    # VERIFY. The whole defect was a command that reported success and did nothing.
    if [ "$(hostname)" = "$NEWHOST" ]; then
      log "hostname -> $NEWHOST"
    else
      log "ERROR: hostname is still $(hostname), wanted $NEWHOST - a second bridge on this LAN would collide on mDNS"
    fi
  fi
else
  log "no pairing code and no CPU serial - cannot derive a name"
fi

# Version stamp. /etc/bridge is bind-mounted from /data/etc-bridge, so the copy the image
# build writes into the rootfs is invisible at runtime; build-disk-image.sh seeds the bind
# source instead. Only fill in a fallback if that seed is somehow absent.
if [ -n "${BRIDGE_VERSION:-}" ]; then
  echo "$BRIDGE_VERSION" >/etc/bridge/version 2>/dev/null || true
elif [ ! -s /etc/bridge/version ]; then
  echo dev >/etc/bridge/version 2>/dev/null || true
  log "version: no seed on /data and none provisioned -> dev"
fi
exit 0
