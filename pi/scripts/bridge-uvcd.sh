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
# The pump prints "/dev/video40: unable to dequeue buffer index N/2 (11)" every time it polls
# before a new frame exists (EAGAIN) - dozens of lines a second. journald spent CPU and SD
# writes on them, flight-recorder greps this journal every second, and they pushed the useful
# "pump:" telemetry out of every log tail. Drop exactly that line (errno 11 only); every other
# line, including dequeue failures with any other errno, still reaches the journal.
# It is a printf() in libuvcgadget's v4l2.c, so it arrives on STDOUT; the "pump:" telemetry is
# fprintf(stderr) and is left untouched. (The first version of this filter sat on stderr and
# removed nothing - found on the live bridge 2026-09-22.)
# SIGPIPE is ignored (inherited across exec) so that if the filter ever died, uvc-gadget would
# get EPIPE on its stdout writes instead of being killed.
trap '' PIPE
exec stdbuf -oL -eL /usr/local/bin/uvc-gadget -d /dev/video40 uvc.0 \
  > >(exec grep --line-buffered -v 'unable to dequeue buffer index [0-9]*/[0-9]* (11)$')
