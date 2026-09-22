#!/usr/bin/env python3
"""Flight recorder: one line per second -> /home/pi/flight.txt (ring, last 500).

    HH:MM:SS up=<s> udc=<state> thr=0x<mask> pull=<0|1>

  pull=1  the client is actively pulling video frames right now (a "pump: ok=" line from
          bridge-uvcd in the last 3 s).

Why Python and not the old shell loop (2026-09-22): the loop launched ~10 programs every
second (journalctl, grep, date, cut x2, cat, vcgencmd, sync, wc, sleep) and re-read the last
3 s of the camera journal each time - measured at ~11% of a core on a 900 MHz bridge. Its
`sync` also flushed EVERY dirty page on the system to the SD card once a second. This keeps
the same line format and the same black-box guarantee (the line is on disk before the next
second starts) with no launches per second:
  - thr   still from vcgencmd (the 6.12 kernel has no sysfs get_throttled node; checked
          on a live bridge 2026-09-22) - the one launch left per second
  - pull  from ONE long-running `journalctl -f` follower instead of a re-scan per second
  - the line is fdatasync'd - only this file, not the whole system
"""
import glob, os, subprocess, threading, time

F = "/home/pi/flight.txt"
KEEP, CAP = 500, 600
PULL_WINDOW = 3.0

_last_pump = [0.0]
_datasync = getattr(os, "fdatasync", os.fsync)     # Linux has fdatasync; fsync elsewhere


def follow_pump():
    """Stamp _last_pump whenever the camera pump reports it served frames."""
    while True:
        try:
            p = subprocess.Popen(["journalctl", "-f", "-n", "0", "-u", "bridge-uvcd", "-o", "cat"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, errors="replace")
            for line in p.stdout:
                if "pump: ok=" in line:
                    _last_pump[0] = time.monotonic()
            p.wait()
        except Exception:
            pass
        time.sleep(5)          # journalctl exited (journald restart): follow again


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def udc_state():
    return " ".join(read(p) for p in sorted(glob.glob("/sys/class/udc/*/state")))


def throttled():
    """Mask as vcgencmd prints it ('0x50005'); '' if unreadable."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                             timeout=3).stdout
        return out.strip().split("=", 1)[1] if "=" in out else ""
    except Exception:
        return ""


def main():
    real = os.path.realpath(F)
    tmp = os.path.join(os.path.dirname(real), ".flight.rotate.tmp")
    threading.Thread(target=follow_pump, daemon=True).start()
    try:
        with open(real) as f:
            n = sum(1 for _ in f)
    except OSError:
        n = 0
    nxt = time.monotonic()
    while True:
        up = read("/proc/uptime").split(".", 1)[0]
        pull = 1 if time.monotonic() - _last_pump[0] < PULL_WINDOW else 0
        line = "%s up=%s udc=%s thr=%s pull=%d\n" % (
            time.strftime("%H:%M:%S"), up, udc_state(), throttled(), pull)
        try:
            with open(real, "a") as f:
                f.write(line)
                f.flush()
                _datasync(f.fileno())
            n += 1
            if n > CAP:
                with open(real) as f:
                    keep = f.readlines()[-KEEP:]
                with open(tmp, "w") as f:
                    f.writelines(keep)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, real)
                n = len(keep)
        except Exception:
            pass               # /data missing or full, or anything else: keep sampling, never exit
        nxt += 1.0
        now = time.monotonic()
        if nxt < now:          # fell behind (a stall): resync rather than burst
            nxt = now + 1.0
        time.sleep(nxt - now)


if __name__ == "__main__":
    main()
