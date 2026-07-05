#!/bin/bash
echo "=== NetBridge status ==="
echo "uptime=$(cut -d' ' -f1 /proc/uptime)s  $(vcgencmd get_throttled)  $(vcgencmd measure_temp)"
echo "UDC=$(cat /sys/class/udc/*/state 2>/dev/null | head -1)   (configured = host attached)"
echo "gadget=$(ls /sys/kernel/config/usb_gadget/g1/functions/ 2>/dev/null | tr '\n' ' ')"
echo "UAC2card=$(cat /proc/asound/cards 2>/dev/null | grep -i uac | xargs || echo none)"
for s in bridge-gadget bridge-feeder-net bridge-feeder-audio bridge-uvcd; do echo "  $s=$(systemctl is-active $s)"; done
echo "wifi=$(awk 'NR>2{print $4}' /proc/net/wireless) dBm"
