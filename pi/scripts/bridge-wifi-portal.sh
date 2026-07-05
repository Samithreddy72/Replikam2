#!/bin/bash
# NetBridge WiFi setup portal — IoT-style onboarding (balena wifi-connect).
#
# When the Pi has NO internet (moved to a new location, no known WiFi in range), this raises a
# captive-portal hotspot "BridgeSetup-<pairing-code>" on wlan0. You connect a phone/laptop to it,
# a portal pops up, you pick the WiFi + enter the password; NetworkManager saves it, the Pi joins,
# and Tailscale reconnects it to the fleet. While the Pi IS online (Ethernet or a known WiFi) this
# stays dormant and never touches the network. The AP only ever uses wlan0, so it never disturbs
# an eth0 uplink (your remote lifeline).
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

PORTAL_IFACE=wlan0
CHECK_INTERVAL=30      # seconds between connectivity checks while online
PORTAL_TIMEOUT=600     # wifi-connect exits after this many seconds idle, then we re-check

have_internet() {
  [ "$(nmcli -t -f CONNECTIVITY general status 2>/dev/null)" = full ] && return 0
  ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && return 0
  return 1
}

ap_ssid() {
  local pc
  pc=$(curl -s --max-time 2 http://localhost:8080/api/status 2>/dev/null \
       | grep -o '"pairing_code": *"[^"]*"' | cut -d'"' -f4)
  echo "BridgeSetup-${pc:-Pi}"
}

# Give NetworkManager time to (re)connect to a known WiFi / Ethernet on boot before deciding
# we're stranded — avoids racing a normal connect.
sleep 60

while true; do
  if have_internet; then
    sleep "$CHECK_INTERVAL"
    continue
  fi
  ssid="$(ap_ssid)"
  logger -t bridge-wifi-portal "offline -> raising setup hotspot '$ssid' on $PORTAL_IFACE"
  # Open AP (no passphrase) for easy onboarding; captive portal on :80. wifi-connect exits when
  # the user provisions a WiFi (NM connects) OR after PORTAL_TIMEOUT idle; the loop then re-checks.
  # --ui-directory must be ABSOLUTE: wifi-connect's default is the relative path "ui", which
  # resolves against the cwd (systemd runs us with cwd=/) and 404s the portal page. The UI assets
  # ship in a SEPARATE wifi-connect-ui.tar.gz (the per-arch binary tarball has none); deploy.sh
  # installs them here.
  wifi-connect --portal-interface "$PORTAL_IFACE" --portal-ssid "$ssid" \
               --ui-directory /usr/local/share/wifi-connect/ui \
               --activity-timeout "$PORTAL_TIMEOUT" 2>&1 | logger -t bridge-wifi-portal
  sleep 5
done
