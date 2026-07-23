#!/bin/bash
LOG(){ logger -t powertrim "$*"; }
# 1) HDMI off (headless) ~30mA
vcgencmd display_power 0 >/dev/null 2>&1 && LOG "hdmi off"
# 2) activity LED off
echo mmc0 > /sys/class/leds/ACT/trigger 2>/dev/null   # LED ON: green blinks on SD activity
# 3) cap CPU to 1.2GHz — kills the big current SPIKES (bridge load is light; decode uses ~5%)
for p in /sys/devices/system/cpu/cpufreq/policy*; do echo 900000 > $p/scaling_max_freq 2>/dev/null; done
LOG "cpu capped 1.2GHz"
# 3b) eth PHY + PWR LED off
ip link set eth0 down 2>/dev/null && LOG "eth0 down"
echo default-on > /sys/class/leds/PWR/trigger 2>/dev/null  # LED ON: red power light solid
# 4) power down the UNUSED USB-A host controller (VL805) ~100-200mA — biggest saver.
#    (nothing is plugged into the USB-A ports; USB-C gadget port is unaffected. reboot restores.)
if [ -e /sys/bus/pci/devices/0000:01:00.0/driver/unbind ]; then
  echo 0000:01:00.0 > /sys/bus/pci/devices/0000:01:00.0/driver/unbind 2>/dev/null && LOG "usb-A host powered down"
fi
