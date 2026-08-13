#!/usr/bin/env python3
"""Close the USB audio rate loop that the kernel leaves open.

THE PROBLEM THIS EXISTS FOR
---------------------------
The meeting laptop sends room audio to this bridge on its own clock. The bridge reads it on
a different clock. Nothing reconciles the two, so the difference accumulates in the capture
ring until a chunk is thrown away. Measured on this hardware while streaming:

    0.710 ms/s of audio never captured, 24 of 67 sample windows short, worst window ~5ms
    mean +/-16ppm -> centred, so this is episodic LOSS, not steady drift

Raising the receive buffer 250 -> 400 -> 600ms changed nothing, and that is the tell: a
jitter buffer repairs audio that arrived late, and cannot repair audio that was never
recorded.

WHY NEITHER GADGET MODE FIXES IT ON ITS OWN
-------------------------------------------
USB Audio Class 2 has a mechanism for exactly this — an isochronous feedback endpoint, where
the device tells the host "you are sending 12ppm too fast, slow down". The kernel implements
the endpoint. It does NOT implement the controller:

    u_audio.c:674     prm->pitch = 1000000;      <- set once, at stream start
    u_audio.c:1146    "Capture Pitch 1000000"    <- an ALSA control; USERSPACE writes it

Nothing in the kernel measures drift and updates that value. (An in-kernel implementation
was added upstream and later reverted.) So:

    c_sync=adaptive   no feedback endpoint at all      -> host never corrects
    c_sync=async      feedback endpoint reports a
                      CONSTANT 1000000 forever         -> host still never corrects

Both lose audio, for the same reason: the loop is open. That is what this daemon closes.

HOW
---
`avail` in the PCM status file is the number of frames the hardware has delivered that the
application has not read yet — the fill level of the ring, and therefore the direct measure
of whether the host is outrunning us. Hold it near a target and nothing accumulates:

    avail above target  -> host is too fast -> pitch below 1000000 -> host slows down
    avail below target  -> host is too slow -> pitch above 1000000 -> host speeds up

The kernel clamps to (1000 - 250)*1000 .. (1000 + fb_max)*1000, i.e. 750000..1005000 with
fb_max=5. Deliberately asymmetric: the host can be slowed a long way and sped up barely at
all, because overrun (audio thrown away) is the failure that matters and underrun merely
costs a little latency. The controller is biased to sit slightly slow for the same reason.

SAFETY
------
It only acts when the stream is RUNNING and the control exists (i.e. c_sync=async). In
adaptive mode the control is absent and this exits quietly rather than fighting a mechanism
that is not there. Every correction is clamped and slew-limited, so a bad reading moves the
host by a fraction of a percent, not a lurch — an audio controller that can oscillate is
worse than none, which is the same lesson the jitter sentry taught.
"""
import argparse, os, re, subprocess, sys, time

CARD = os.environ.get("PITCH_CARD", "UAC2Gadget")
STATUS = "/proc/asound/%s/pcm0c/sub0/status" % CARD
CTL = "Capture Pitch 1000000"

NOMINAL = 1000000
PITCH_MIN = 750000          # (1000 - FBACK_SLOW_MAX) * 1000, FBACK_SLOW_MAX = 250
PITCH_MAX = 1005000         # (1000 + fb_max) * 1000, fb_max = 5

# Target ring fill. alsasrc opens with buffer-time=200000 (200ms) and latency-time=20000
# (20ms period) => ~9600 frames of buffer, ~960 per period. Two periods of headroom keeps us
# clear of underrun without sitting so full that a burst overruns.
TARGET_FRAMES = int(os.environ.get("PITCH_TARGET_FRAMES", "1920"))
GAIN = float(os.environ.get("PITCH_GAIN", "3.0"))       # ppm-ish per frame of error
MAX_STEP = int(os.environ.get("PITCH_MAX_STEP", "2000"))  # per update, out of 1000000
PERIOD_S = float(os.environ.get("PITCH_PERIOD_S", "1.0"))


def log(*a):
    print("bridge-pitch:", *a, file=sys.stderr, flush=True)


def read_status():
    """(state, avail, hw_ptr) or (None, None, None) when the stream is not open."""
    try:
        with open(STATUS) as f:
            txt = f.read()
    except Exception:
        return None, None, None
    if not txt or txt.startswith("closed"):
        return None, None, None
    state = avail = hw = None
    for ln in txt.splitlines():
        if ln.startswith("state:"):
            state = ln.split(":", 1)[1].strip()
        elif ln.startswith("avail"):
            m = re.search(r"(\d+)", ln)
            if m:
                avail = int(m.group(1))
        elif ln.startswith("hw_ptr"):
            m = re.search(r"(\d+)", ln)
            if m:
                hw = int(m.group(1))
    return state, avail, hw


def ctl_exists():
    """True only when the gadget is in async mode — the control is created with the feedback
    endpoint. Its absence is the honest signal that there is nothing to drive."""
    try:
        r = subprocess.run(["amixer", "-c", CARD, "controls"],
                           capture_output=True, text=True, timeout=5)
        return CTL in (r.stdout or "")
    except Exception:
        return False


def set_pitch(v):
    try:
        subprocess.run(["amixer", "-c", CARD, "cset",
                        "iface=PCM,name=%s" % CTL, str(v)],
                       capture_output=True, text=True, timeout=5)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one measurement, print, exit")
    a = ap.parse_args()

    # WAIT FOR THE GADGET. This unit can start before bridge-gadget has finished building
    # the card, and a controller that gives up in the first second would simply never run —
    # silently, in exactly the configuration it was written for.
    deadline = time.time() + float(os.environ.get("PITCH_WAIT_S", "60"))
    while time.time() < deadline:
        if os.path.exists("/proc/asound/%s" % CARD) and ctl_exists():
            break
        time.sleep(2)

    if not ctl_exists():
        # Adaptive mode has no feedback endpoint, so this control does not exist. That is a
        # legitimate configuration, not a fault: exit 0 so systemd does not restart-loop.
        log("control '%s' not present on card %s — gadget is not in async mode; "
            "nothing to drive. Exiting cleanly." % (CTL, CARD))
        return 0

    log("controlling %s: target avail=%d frames, gain=%.1f, clamp %d..%d"
        % (CTL, TARGET_FRAMES, GAIN, PITCH_MIN, PITCH_MAX))

    pitch = NOMINAL
    set_pitch(pitch)
    settled = 0

    while True:
        state, avail, hw = read_status()
        if state != "RUNNING" or avail is None:
            # Stream closed or idle. Reset to nominal so the next session starts from a known
            # place rather than inheriting a correction computed for a different clock.
            if pitch != NOMINAL:
                pitch = NOMINAL
                set_pitch(pitch)
                log("stream idle — pitch reset to nominal")
            if a.once:
                print("stream not running"); return 1
            time.sleep(PERIOD_S)
            continue

        err = avail - TARGET_FRAMES          # >0 means the host is outrunning us
        want = NOMINAL - int(GAIN * err)
        want = max(PITCH_MIN, min(PITCH_MAX, want))
        # Slew limit. A single odd reading — a scheduling hiccup, a stat that landed mid
        # update — must not yank the host's clock.
        if want > pitch + MAX_STEP:
            want = pitch + MAX_STEP
        elif want < pitch - MAX_STEP:
            want = pitch - MAX_STEP

        if a.once:
            print("avail=%s err=%+d pitch=%d (%+d ppm)" % (avail, err, want, want - NOMINAL))
            return 0

        if want != pitch:
            pitch = want
            set_pitch(pitch)
            settled = 0
        else:
            settled += 1
            if settled == 30:
                log("holding at %d (%+d ppm), avail=%d" % (pitch, pitch - NOMINAL, avail))

        time.sleep(PERIOD_S)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
