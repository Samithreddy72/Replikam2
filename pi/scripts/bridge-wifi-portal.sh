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

PASS_FILE=/etc/bridge/setup-wifi-pass

ap_ssid() {
  local pc
  pc=$(curl -s --max-time 2 http://localhost:8080/api/status 2>/dev/null \
       | grep -o '"pairing_code": *"[^"]*"' | cut -d'"' -f4)
  # The pairing code already starts with "BRIDGE-", so a naive prefix produced
  # "BridgeSetup-BRIDGE-2626" - confusing on a phone's WiFi list, and it cost a whole
  # flash cycle on 2026-07-24 when the AP was up but nobody recognised the name.
  echo "BridgeSetup-${pc:-Pi}" | sed "s/BridgeSetup-BRIDGE-/BridgeSetup-/"
}

# The setup AP's WPA2 key. MUST NOT be derivable from the broadcast SSID — it
# used to be `sed s/BridgeSetup-/BRIDGE-/` of the SSID, which meant anyone in
# radio range could compute it from the beacon and sit on the portal while the
# receiver typed the venue's WiFi password. Now: 12 random chars, generated once
# per device on first use and persisted, so it matches the printed label for the
# life of the card. Regenerating would strand the label, hence create-if-absent.
# The setup-AP WPA2 key. Fixed default "bridge2626" (>=8 chars, always valid). CRITICAL: this
# must NEVER return empty — wifi-connect rejects a 0-length password and the AP never appears
# (the 2026-07-24 defect: /etc/bridge was read-only, the file was empty, the portal looped).
# So: use the persisted file if it has content; else try to write the default; and no matter
# what, echo a valid password so the AP always comes up.
SETUP_PASS_DEFAULT="bridge2626"
ap_pass() {
  if [ -s "$PASS_FILE" ]; then cat "$PASS_FILE"; return; fi
  install -d -m 755 /etc/bridge 2>/dev/null || true
  if printf '%s' "$SETUP_PASS_DEFAULT" > "$PASS_FILE" 2>/dev/null; then
    chmod 600 "$PASS_FILE" 2>/dev/null || true
    logger -t bridge-wifi-portal "wrote setup-AP passphrase to $PASS_FILE"
    cat "$PASS_FILE"
  else
    logger -t bridge-wifi-portal "WARN: $PASS_FILE unwritable — using built-in default"
    printf '%s' "$SETUP_PASS_DEFAULT"
  fi
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
  # WPA2-protected with the per-device random key printed on the device label —
  # nobody nearby can join the setup AP or watch credentials being entered.
  # CRITICAL: wifi-connect saves the setup AP as a NetworkManager profile, and
  # /etc/NetworkManager/system-connections is bind-mounted to /data, so it PERSISTS.
  # On the next boot NM auto-activates it, wlan0 comes up in AP mode, and:
  #   - a radio in AP mode cannot scan, so the portal lists only ITSELF and the user
  #     can never pick their real network ("i cant see other wifi's", 2026-07-24);
  #   - it also steals the radio back after a successful join, knocking the device off
  #     the network it had just connected to (seen as a 30-second lease at .184).
  # Purge every stale BridgeSetup-* profile and rescan in station mode before raising.
  nmcli -t -f NAME connection show 2>/dev/null | grep '^BridgeSetup-' | while read -r _c; do
    logger -t bridge-wifi-portal "deleting stale setup-AP profile '$_c'"
    nmcli connection delete "$_c" >/dev/null 2>&1 || true
  done
  nmcli device wifi rescan >/dev/null 2>&1 || true
  sleep 3
  pass="$(ap_pass)"
  wifi-connect --portal-interface "$PORTAL_IFACE" --portal-ssid "$ssid" \
               --portal-passphrase "$pass" \
               --ui-directory /usr/local/share/wifi-connect/ui \
               --activity-timeout "$PORTAL_TIMEOUT" 2>&1 | logger -t bridge-wifi-portal
  # wifi-connect exits as soon as it hands credentials to NetworkManager; NM still needs
  # ~15-30s to associate + DHCP. Waiting only 5s here re-raised the AP mid-handshake and
  # killed the connection ("unable to connect" - 2026-07-24). Poll up to 60s before re-raising.
  # Never let the setup AP outlive this session — otherwise it persists to /data and
  # poisons every later boot (see above).
  nmcli -t -f NAME connection show 2>/dev/null | grep '^BridgeSetup-' | while read -r _c; do
    nmcli connection modify "$_c" connection.autoconnect no >/dev/null 2>&1 || true
    nmcli connection delete "$_c" >/dev/null 2>&1 || true
  done
  for _i in $(seq 1 12); do
    sleep 5
    if have_internet; then
      logger -t bridge-wifi-portal "connected after portal handoff - not re-raising AP"
      break
    fi
  done
done
