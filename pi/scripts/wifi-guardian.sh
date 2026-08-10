#!/bin/bash
# RepliKam Wi-Fi Guardian v4 — office-grade. Keeps wlan0 + tailscale alive on ANY network.
IW=/usr/sbin/iw
LOG(){ logger -t wifi-guardian "$*"; }
NETLOG=/home/pi/netlog.txt
# Same read-only-root trap as flight-recorder.sh: /home/pi/netlog.txt is redirected onto
# /data so the append works, but "$NETLOG.tmp" lands in /home/pi, which is read-only. The
# ring rotation failed every single time and logged "Read-only file system" for it.
NETLOG_REAL="$(readlink -f "$NETLOG" 2>/dev/null || echo "$NETLOG")"
NETLOG_TMP="$(dirname "$NETLOG_REAL")/.netlog.rotate.tmp"

for i in $(seq 1 30); do [ -d /sys/class/net/wlan0 ] && break; sleep 2; done
$IW dev wlan0 set power_save off 2>/dev/null && LOG "power_save off"

online() {
  # multi-signal liveness: corporate networks often block ICMP to the gateway —
  # never declare "down" on one blocked path.
  local GW=$(ip route | awk '/default/ {print $3; exit}')
  [ -n "$GW" ] && ping -c1 -W2 -I wlan0 "$GW" >/dev/null 2>&1 && return 0
  ping -c1 -W2 -I wlan0 1.1.1.1 >/dev/null 2>&1 && return 0
  ping -c1 -W2 -I wlan0 8.8.8.8 >/dev/null 2>&1 && return 0
  # NM's HTTP connectivity check works where ICMP is blocked
  case "$(nmcli -t -f CONNECTIVITY general status 2>/dev/null)" in full|limited|portal) return 0;; esac
  # tailscale control reachability = definitely online
  timeout 6 tailscale status --peers=false >/dev/null 2>&1 && return 0
  return 1
}

fails=0; tick=0
while true; do
  tick=$((tick+1))
  if online; then
    fails=0
  else
    fails=$((fails+1))
    LOG "liveness miss #$fails (ssid=$(/usr/sbin/iw dev wlan0 link 2>/dev/null | awk -F': ' '/SSID/{print $2}'))"
    if [ "$fails" -eq 3 ]; then
      LOG "reconnect wlan0"
      nmcli device disconnect wlan0 >/dev/null 2>&1; sleep 2
      if ! nmcli device connect wlan0 >/dev/null 2>&1; then
        visible=$(nmcli -t -f SSID dev wifi list ifname wlan0 --rescan yes 2>/dev/null | sort -u)
        nmcli -t -f NAME,TYPE con show | grep wireless | cut -d: -f1 | while read -r con; do
          echo "$visible" | grep -qxF "$con" && { LOG "trying saved $con"; nmcli con up "$con" >/dev/null 2>&1 && break; }
        done
      fi
    elif [ "$fails" -eq 8 ]; then
      LOG "radio bounce"; nmcli radio wifi off; sleep 3; nmcli radio wifi on; sleep 8
      nmcli device connect wlan0 >/dev/null 2>&1
    elif [ "$fails" -eq 20 ] || [ "$fails" -eq 60 ]; then
      LOG "restart NetworkManager"; systemctl restart NetworkManager; sleep 15
    elif [ "$fails" -ge 120 ]; then
      if grep -q configured /sys/class/udc/*/state 2>/dev/null; then
        LOG "offline 10min but CLIENT ATTACHED - refusing reboot (meeting-safe); keep reconnecting"
        fails=60
      else
        LOG "LAST RESORT: ~10min offline, no client -> clean reboot"; sync; systemctl reboot
      fi
    fi
    $IW dev wlan0 set power_save off 2>/dev/null
  fi
  # tailscale self-heal every ~60s (remote access must always work)
  if [ $((tick % 12)) -eq 0 ]; then
    if ! systemctl is-active tailscaled >/dev/null 2>&1; then
      LOG "tailscaled dead -> restart"; systemctl restart tailscaled; sleep 5; timeout 15 tailscale up 2>/dev/null
    elif online && ! timeout 8 tailscale status --peers=false >/dev/null 2>&1; then
      LOG "tailscale wedged -> up"; timeout 15 tailscale up 2>/dev/null
    fi
  fi
  # forensic netlog every ~30s (ring: keep last 400 lines)
  if [ $((tick % 6)) -eq 0 ]; then
    echo "$(date "+%H:%M:%S") ssid=$(/usr/sbin/iw dev wlan0 link 2>/dev/null | awk -F': ' '/SSID/{print $2}') ip=$(ip -o -4 addr show wlan0 2>/dev/null | awk '{print $4}') sig=$(/usr/sbin/iw dev wlan0 link 2>/dev/null | awk '/signal/{print $2}')dBm nm=$(nmcli -t -f CONNECTIVITY general status 2>/dev/null) ts=$(timeout 4 tailscale status --peers=false >/dev/null 2>&1 && echo up || echo down) fails=$fails" >> $NETLOG
    tail -400 "$NETLOG_REAL" > "$NETLOG_TMP" 2>/dev/null && mv -f "$NETLOG_TMP" "$NETLOG_REAL" 2>/dev/null
  fi
  sleep 5
done
