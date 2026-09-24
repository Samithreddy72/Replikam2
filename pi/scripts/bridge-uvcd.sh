#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
# USB interrupt placement. The Pi 4's DWC2 video endpoint has tight isochronous deadlines; with
# its IRQ on CPU 0 (the kernel default) WaysToGo measured 78 "-61" missed transfers per 20 s,
# on CPU 2 1-5 (2026-09-16). This is the setting of the verified working configuration of
# 2026-09-24 (docs/WORKING-CONFIG-2026-09-24.md). "0-3" restores the kernel default for A/B tests.
USB_IRQ_CPUS="2"
for usb_irq in $(awk '/fe980000[.]usb/ {gsub(":", "", $1); print $1}' /proc/interrupts); do
  if printf '%s\n' "$USB_IRQ_CPUS" > "/proc/irq/$usb_irq/smp_affinity_list" 2>/dev/null; then
    echo "uvcd: DWC2 IRQ $usb_irq affinity = $USB_IRQ_CPUS" >&2
  else
    echo "uvcd: could not set DWC2 IRQ $usb_irq affinity; retaining system setting" >&2
  fi
done

# USB video miss counter (read-only). journald no longer ingests kernel messages, so read the
# kernel's own buffer (/dev/kmsg) from "now" onwards and write, every 10 s, how many isochronous
# video transfers failed and which CPUs serviced the USB interrupt:
#   /run/netbridge-usb-video.txt   "HH:MM:SS enodata=N exdev=N other=N irq_cpu=c0/c1/c2/c3 variant=default"
# Costs nothing measurable: it sleeps in select() and wakes only for kernel messages.
KMSG="${KMSG:-/dev/kmsg}" PROC_INT="${PROC_INT:-/proc/interrupts}" USB_STATS="${USB_STATS:-/run/netbridge-usb-video.txt}" \
VARIANT="default" python3 - <<'COUNTER' >/dev/null 2>&1 &
import os, select, time
kmsg, pint, out, var = os.environ["KMSG"], os.environ["PROC_INT"], os.environ["USB_STATS"], os.environ["VARIANT"]
def irq_counts():
    try:
        for ln in open(pint):
            if "fe980000.usb" in ln:
                return [int(x) for x in ln.split()[1:5] if x.isdigit()]
    except OSError:
        pass
    return []
fd = os.open(kmsg, os.O_RDONLY | os.O_NONBLOCK)
try:
    os.lseek(fd, 0, os.SEEK_END)          # only messages from now on
except OSError:
    pass
c = {"enodata": 0, "exdev": 0, "other": 0}
prev = irq_counts(); t_next = time.monotonic() + 10
while True:
    r, _, _ = select.select([fd], [], [], max(0.0, t_next - time.monotonic()))
    if r:
        try:
            rec = os.read(fd, 8192).decode("utf-8", "replace")
        except BlockingIOError:
            rec = ""
        except OSError:                   # EPIPE: ring overwritten under us - keep going
            rec = ""
        if "VS request" in rec or "missed xfer" in rec:
            if "status -61" in rec: c["enodata"] += 1
            elif "status -18" in rec or "missed xfer" in rec: c["exdev"] += 1
            else: c["other"] += 1
        elif not rec and not r:
            pass
    if time.monotonic() >= t_next:
        now = irq_counts(); d = [b - a for a, b in zip(prev, now)] if prev and now else []
        line = "%s enodata=%d exdev=%d other=%d irq_cpu=%s variant=%s\n" % (
            time.strftime("%H:%M:%S"), c["enodata"], c["exdev"], c["other"],
            "/".join(str(x) for x in d) or "?", var)
        try:
            lines = (open(out).read().splitlines(True) if os.path.exists(out) else [])[-359:]
            with open(out + ".tmp", "w") as f:
                f.writelines(lines + [line])
            os.replace(out + ".tmp", out)
        except OSError:
            pass
        c = {"enodata": 0, "exdev": 0, "other": 0}; prev = now; t_next += 10
COUNTER

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
