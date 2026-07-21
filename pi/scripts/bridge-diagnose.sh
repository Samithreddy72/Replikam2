#!/bin/bash
# Collects a complete diagnostics bundle: service states, logs, USB/gadget state,
# network history, flight recorder, audio clock stats. Output: /home/pi/diagnostics/
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/bin
TS=$(date +%Y%m%d-%H%M%S)
D=/tmp/diag-$TS; mkdir -p $D /home/pi/diagnostics
{ uname -a; uptime; echo; systemctl status bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio wifi-guardian jitter-sentry --no-pager -l 2>/dev/null | head -120; } > $D/services.txt
journalctl -b --no-pager -n 2000 > $D/journal-tail.txt 2>/dev/null
dmesg 2>/dev/null | tail -300 > $D/dmesg.txt
cp /home/pi/flight.txt /home/pi/netlog.txt $D/ 2>/dev/null
{ echo "UDC: $(cat /sys/class/udc/*/state 2>/dev/null)"; echo "gadget: $(cat /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null)"; ls -la /dev/video* 2>/dev/null; cat /proc/asound/card*/pcm0p/sub0/status 2>/dev/null; echo ---; cat /proc/asound/card*/pcm0c/sub0/status 2>/dev/null; } > $D/usb-av-state.txt
{ nmcli con show --active; /usr/sbin/iw dev wlan0 link; ip -4 addr; } > $D/network.txt 2>&1
{ echo "undervolt=$(cat /sys/class/hwmon/hwmon*/in0_lcrit_alarm 2>/dev/null | head -1)"; journalctl --since -86400s --no-pager 2>/dev/null | grep -i voltage | tail -20; } > $D/power.txt
# clock FFT (M4): a real spectral+click verdict on the return-audio path. Capture a
# short snippet if the device is free (idle bridge); either way fold in the sentry's
# latest latched verdict. This is the "clock FFT" line in the walkthrough bundle.
{
  W=$(mktemp /tmp/diag-cap-XXXX.wav)
  if arecord -D hw:UAC2Gadget -d 2 -f S16_LE -r 48000 -c 1 -q "$W" 2>/dev/null && [ -s "$W" ]; then
    echo "== live capture (2s from hw:UAC2Gadget) =="
    /usr/local/bin/bridge-clock-fft.py --ascii "$W" 2>&1
  else
    echo "== no live capture (return-audio holding the device / session live) =="
  fi
  rm -f "$W"
  echo; echo "== sentry latched verdict =="
  cat /run/bridge/crackle.json 2>/dev/null || echo "(none — no crackle currently latched)"
} > $D/clock-fft.txt 2>&1
tar czf /home/pi/diagnostics/bundle-$TS.tgz -C /tmp diag-$TS 2>/dev/null
rm -rf $D
ls -t /home/pi/diagnostics/*.tgz | tail -n +6 | xargs rm -f 2>/dev/null   # keep last 5
echo "bundle: /home/pi/diagnostics/bundle-$TS.tgz ($(stat -c%s /home/pi/diagnostics/bundle-$TS.tgz) bytes)"
