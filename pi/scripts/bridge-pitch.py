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
The documented mechanism is feed-FORWARD, not a servo: "userspace calculates the real
sampling frequency at which it consumes samples, then tells the real sampling frequency to
the UAC2 gadget driver, which notifies the host". So that is what this measures.

`appl_ptr` counts frames THIS SIDE has read. Its slope over a long window IS our true
consumption rate. Report that as a ratio of nominal and the host matches it:

    pitch = 1000000 * (measured_consumption_rate / nominal_rate)

An earlier version of this file servoed on `avail` (ring fill) instead, and real data from
the card killed that design before it ever ran:

    avail : 864      avail_max : 960

`avail` never exceeds one period, because alsasrc drains a period at a time — it sawtooths
between 0 and 960 rather than settling anywhere. A fill-level target of 1920 was therefore
UNREACHABLE, the error would have stayed permanently negative, and the controller would have
pinned the host at +0.5% forever while believing it was correcting. Measuring a slope over
30 seconds is immune to that sawtooth; a fill target is not.

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

NOMINAL = 1000000                       # the pitch control is a ratio x 1e6
NOMINAL_RATE = int(os.environ.get("PITCH_NOMINAL_RATE", "48000"))
PITCH_MIN = 750000          # (1000 - FBACK_SLOW_MAX) * 1000, FBACK_SLOW_MAX = 250
PITCH_MAX = 1005000         # (1000 + fb_max) * 1000, fb_max = 5

# Averaging window. Real clock drift is tens of ppm and changes slowly, so measure over a
# long window: the longer it is, the less the per-period sawtooth and scheduling noise matter.
# 30s of 48kHz is 1.44M frames, so a one-frame miscount is 0.7ppm.
WINDOW_S = float(os.environ.get("PITCH_WINDOW_S", "30"))
PERIOD_S = float(os.environ.get("PITCH_PERIOD_S", "1.0"))
# Never move the host by more than this from nominal. Crystals drift by tens of ppm, not
# thousands; anything larger is a measurement fault, not a clock, and must not be obeyed.
MAX_PPM = int(os.environ.get("PITCH_MAX_PPM", "1000"))
MAX_STEP = int(os.environ.get("PITCH_MAX_STEP", "200"))   # ppm per update

def log(*a):
    print("bridge-pitch:", *a, file=sys.stderr, flush=True)


def read_status():
    """(state, avail, hw_ptr, appl_ptr), or Nones when the stream is not open."""
    try:
        with open(STATUS) as f:
            txt = f.read()
    except Exception:
        return None, None, None, None
    if not txt or txt.startswith("closed"):
        return None, None, None, None
    state = avail = hw = appl = None
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
        elif ln.startswith("appl_ptr"):
            m = re.search(r"(\d+)", ln)
            if m:
                appl = int(m.group(1))
    return state, avail, hw, appl


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

    # This line survived the rewrite that removed the ring-fill target and still named
    # TARGET_FRAMES and GAIN, neither of which exists any more. It sits immediately after the
    # async-mode check, so it never ran while the image shipped c_sync=adaptive — and would
    # have raised NameError the first time anyone used gadget-tune:async. Report the
    # parameters this controller actually has.
    log("controlling %s: window=%.0fs  max=%dppm  step=%dppm  clamp %d..%d"
        % (CTL, WINDOW_S, MAX_PPM, MAX_STEP, PITCH_MIN, PITCH_MAX))

    pitch = NOMINAL
    set_pitch(pitch)
    hist = []          # (monotonic, appl_ptr)
    last_log = 0.0

    while True:
        state, avail, hw, appl = read_status()
        now = time.monotonic()

        if state != "RUNNING" or appl is None:
            # Idle or closed. Forget the history and return to nominal: a correction computed
            # for the last session's clock is meaningless for the next one.
            hist.clear()
            if pitch != NOMINAL:
                pitch = NOMINAL
                set_pitch(pitch)
                log("stream idle — pitch reset to nominal")
            if a.once:
                print("stream not running"); return 1
            time.sleep(PERIOD_S)
            continue

        # A pointer going backwards means the stream restarted underneath us (a rate change,
        # a service restart). Everything measured before that belongs to a different stream.
        if hist and appl < hist[-1][1]:
            log("appl_ptr went backwards — stream restarted, discarding history")
            hist.clear()

        hist.append((now, appl))
        while len(hist) > 2 and (now - hist[0][0]) > WINDOW_S:
            hist.pop(0)

        span = now - hist[0][0]
        if span >= WINDOW_S * 0.8:
            frames = appl - hist[0][1]
            rate = frames / span
            ppm = int(round((rate / NOMINAL_RATE - 1.0) * 1e6))
            ppm = max(-MAX_PPM, min(MAX_PPM, ppm))
            want = NOMINAL + ppm
            want = max(PITCH_MIN, min(PITCH_MAX, want))
            if want > pitch + MAX_STEP:
                want = pitch + MAX_STEP
            elif want < pitch - MAX_STEP:
                want = pitch - MAX_STEP

            if a.once:
                print("rate=%.1f Hz over %.0fs -> %+d ppm (pitch %d), avail=%s"
                      % (rate, span, ppm, want, avail))
                return 0

            if want != pitch:
                pitch = want
                set_pitch(pitch)
            if now - last_log > 60:
                last_log = now
                log("consuming %.1f Hz (%+d ppm), pitch=%d, avail=%s"
                    % (rate, ppm, pitch, avail))
        elif a.once:
            print("need %.0fs of history, have %.0fs" % (WINDOW_S, span)); return 1

        time.sleep(PERIOD_S)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
