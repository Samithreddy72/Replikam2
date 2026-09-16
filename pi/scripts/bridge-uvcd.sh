#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
# Pi 4's DWC2 video endpoint has tight isochronous deadlines. Keeping its IRQ
# off CPU 0 reduced live -ENODATA completions from 78/20s to 1-5/20s.
# Resolve the IRQ number at boot; never assume the current numbering is stable.
# Unsupported hardware or affinity restrictions must not prevent camera startup.
if [ -d /sys/devices/system/cpu/cpu2 ] &&
   { [ ! -e /sys/devices/system/cpu/cpu2/online ] ||
     [ "$(cat /sys/devices/system/cpu/cpu2/online 2>/dev/null)" = 1 ]; }; then
  for usb_irq in $(awk '/fe980000[.]usb/ {gsub(":", "", $1); print $1}' /proc/interrupts); do
    if printf '2\n' > "/proc/irq/$usb_irq/smp_affinity_list" 2>/dev/null; then
      echo "uvcd: DWC2 IRQ $usb_irq assigned to CPU 2" >&2
    else
      echo "uvcd: retaining system IRQ affinity" >&2
    fi
  done
fi
exec stdbuf -oL -eL /usr/local/bin/uvc-gadget -d /dev/video40 uvc.0
