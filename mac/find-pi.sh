#!/usr/bin/env bash
# Locate the RepliKam Pi wherever it is: tailscale -> mDNS -> LAN sweep. Prints the best IP.
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
KEY=~/.ssh/pi_bridge
try(){ ssh -i $KEY -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new pi@$1 true 2>/dev/null && echo "$1" && exit 0; }
# 1) tailscale (works across networks)
try 100.91.108.50
# 2) mDNS name
IP=$(ping -c1 -t2 bridge-001.local 2>/dev/null | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -1); [ -n "$IP" ] && try "$IP"
# 3) sweep the local subnet for a Pi MAC
SUB=$(ipconfig getifaddr en0 2>/dev/null | cut -d. -f1-3)
if [ -n "$SUB" ]; then
  for i in $(seq 1 254); do ping -c1 -W150 $SUB.$i >/dev/null 2>&1 & done; wait 2>/dev/null
  IP=$(arp -a | grep -iE "b8:27:eb|dc:a6:32|e4:5f:01|28:cd:c1|d8:3a:dd|2c:cf:67" | grep -oE '\(([0-9.]+)\)' | tr -d '()' | head -1)
  [ -n "$IP" ] && try "$IP"
fi
echo "NOT FOUND — is the Pi powered + on a network? (its setup hotspot BridgeSetup-XXXX appears if it is online-less)" >&2
exit 1
