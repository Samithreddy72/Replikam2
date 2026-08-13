#!/bin/bash
# Collects a complete diagnostics bundle: service states, logs, USB/gadget state,
# network history, flight recorder, audio clock stats. Output: /home/pi/diagnostics/
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/bin
TS=$(date +%Y%m%d-%H%M%S)
D=/tmp/diag-$TS; mkdir -p $D /home/pi/diagnostics
{ uname -a; uptime; echo; systemctl status bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio bridge-crackle-sentry bridge-supervisor bridge-agent fleet-brain wifi-guardian jitter-sentry --no-pager -l 2>/dev/null | head -160; } > $D/services.txt
journalctl -b --no-pager -n 2000 > $D/journal-tail.txt 2>/dev/null
dmesg 2>/dev/null | tail -300 > $D/dmesg.txt
cp /home/pi/flight.txt /home/pi/netlog.txt $D/ 2>/dev/null
# INTERRUPT LOAD — the number that settles whether the audio gaps come from the USB
# controller. dwc2 has no hardware (u)frame tracking, so any periodic endpoint forces the
# driver to unmask SOF interrupts; on other people's hardware that has measured 250-300k/sec
# and it is host-dependent, which is why it looks intermittent. Two samples a second apart so
# the RATE is readable, not just a since-boot total that means nothing on its own.
{ echo "== /proc/interrupts (sample 1) =="; cat /proc/interrupts
  echo; echo "== 1 second =="; sleep 1
  echo "== /proc/interrupts (sample 2) =="; cat /proc/interrupts
  echo; echo "== softirqs =="; cat /proc/softirqs
  echo; echo "== load =="; cat /proc/loadavg; uptime
} > $D/interrupts.txt 2>/dev/null

{ echo "UDC: $(cat /sys/class/udc/*/state 2>/dev/null)"; echo "gadget: $(cat /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null)"; ls -la /dev/video* 2>/dev/null; cat /proc/asound/card*/pcm0p/sub0/status 2>/dev/null; echo ---; cat /proc/asound/card*/pcm0c/sub0/status 2>/dev/null; } > $D/usb-av-state.txt
{ nmcli con show --active; /usr/sbin/iw dev wlan0 link; ip -4 addr; } > $D/network.txt 2>&1
{ echo "$(vcgencmd get_throttled 2>/dev/null)   # 0x0=ok; bit0 undervolt-now, bit16 undervolt-occurred, bit18 throttled-occurred"; vcgencmd measure_temp 2>/dev/null; echo "undervolt_alarm=$(cat /sys/class/hwmon/hwmon*/in0_lcrit_alarm 2>/dev/null | head -1)"; journalctl --since -86400s --no-pager 2>/dev/null | grep -i voltage | tail -20; } > $D/power.txt
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
# Mesh (phase 5): this bridge's mesh IP + the tailnet peers/paths (direct vs relay,
# tx/rx) + the ephemeral nb-source app nodes currently connected. The state needed
# to debug the embedded-mesh forward/return path without SSHing in.
{ echo "== tailscale ip =="; tailscale ip 2>/dev/null
  echo; echo "== tailscale status (peers, path direct/relay, tx/rx) =="; tailscale status 2>/dev/null
} > $D/mesh.txt 2>&1
# Gadget audio tuning (the music-jitter root cause fix: c_sync=adaptive + req_number)
# + v4l2loopback consumer state (the feeder-starvation / video-to-client path that
# crash-loops when nothing drains /dev/video40).
{ F=/sys/kernel/config/usb_gadget/g1/functions/uac2.usb0
  echo "== UAC2 gadget =="; for k in c_sync c_srate c_chmask req_number p_chmask fb_max; do echo "$k = $(cat $F/$k 2>/dev/null)"; done
  echo; echo "== v4l2loopback =="; echo "max_buffers=$(cat /sys/module/v4l2loopback/parameters/max_buffers 2>/dev/null) exclusive_caps=$(cat /sys/module/v4l2loopback/parameters/exclusive_caps 2>/dev/null)"
  echo "/dev/video40 openers:"; fuser -v /dev/video40 2>&1
  echo; echo "== return-audio peer/config =="; cat /etc/default/bridge-return-audio 2>/dev/null
} > $D/gadget-av.txt 2>&1
# Media health: crash-loop signatures (feeder starving with no consumer) + power
# and load RIGHT NOW — the fingerprints of the brownout / feeder-crash issues.
{ echo "== restart counters (climbing = crash-loop) =="
  for s in bridge-feeder-net bridge-return-audio bridge-feeder-audio bridge-uvcd fleet-brain; do echo "$s NRestarts=$(systemctl show $s -p NRestarts --value 2>/dev/null)"; done
  echo; echo "== load / mem =="; cat /proc/loadavg; free -m 2>/dev/null | head -2
} > $D/media-health.txt 2>&1
tar czf /home/pi/diagnostics/bundle-$TS.tgz -C /tmp diag-$TS 2>/dev/null
rm -rf $D
ls -t /home/pi/diagnostics/*.tgz | tail -n +6 | xargs rm -f 2>/dev/null   # keep last 5
echo "bundle: /home/pi/diagnostics/bundle-$TS.tgz ($(stat -c%s /home/pi/diagnostics/bundle-$TS.tgz) bytes)"
