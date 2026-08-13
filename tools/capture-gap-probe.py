#!/usr/bin/env python3
"""Measure how much audio the bridge FAILS TO CAPTURE, and prove what makes it worse.

WHY
---
Every instrument said the return path was healthy while the audio was audibly broken: 0%
packet loss, low ping jitter, all gates green. Raising the receive buffer 250 -> 400 -> 600ms
changed nothing — which is itself the finding, because a jitter buffer repairs LATE audio and
cannot repair audio that was never captured.

raspberrypi/linux#5188 documents the mechanism: the dwc2 USB gadget controller produces
non-periodic ~1ms gaps in gadget audio capture, ALSA reports no error, and the gaps get worse
with more concurrent USB streams. Our bridge runs a UVC video gadget and a UAC2 audio gadget
on that same controller. The RPi forum thread on CM4 gadget interrupt load explains why:
dwc2 has no hardware (u)frame tracking, so any periodic endpoint forces a SOF interrupt storm,
and dwc2 — unlike dwc3 — has no interrupt moderation to blunt it.

WHAT THIS MEASURES
------------------
`hw_ptr` is the ALSA hardware pointer: frames the capture hardware actually clocked. It
cannot be faked by a reported setting. Over a known wall-clock interval, a healthy 48kHz
capture advances 48000 frames per second. A shortfall is audio that was never recorded.

One sample cannot tell you much — the 2s measurement window quantises to about +/-470ppm.
Many samples can: a stream that is merely noisy scatters symmetrically around zero, while a
stream that is LOSING audio is skewed negative and throws outliers far past the noise floor.

HOW TO SETTLE THE VIDEO QUESTION
--------------------------------
    python3 tools/capture-gap-probe.py --secs 90 --label "camera ON"    # while streaming
    ... turn the meeting camera off, leave audio running ...
    python3 tools/capture-gap-probe.py --secs 90 --label "camera OFF"

Then compare `deficit ms/s`. If it falls sharply with the camera off, the video gadget is
starving the audio gadget on the shared controller and the fix is to cut periodic-endpoint
pressure or move audio off dwc2 entirely. If it does not move, video is not the aggravator.

Needs no privileges and touches nothing on the bridge — it only reads /api/checks, which the
presenter app already polls.
"""
import argparse, json, re, statistics, sys, time, urllib.request

CTRL = "http://127.0.0.1:18080"


def ring(base, timeout=10):
    """Capture-ring pointers, so we can tell WHICH kind of loss this is."""
    try:
        req = urllib.request.Request(base + "/api/status")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read().decode("utf-8", "replace")) or {}).get("pcm")
    except Exception:
        return None


def sample(base, timeout=20):
    """One (frames, seconds, nominal_rate) reading, or None."""
    try:
        req = urllib.request.Request(base + "/api/checks")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    detail = ((d.get("return_audio") or {}).get("detail") or "")
    m = re.search(r"(\d+) frames in ([\d.]+)s.*?@ *(\d+)", detail)
    if not m:
        return None
    return int(m.group(1)), float(m.group(2)), int(m.group(3))


def run(secs, base, label):
    t_end = time.time() + secs
    rows = []
    print("\n  sampling for %ds%s — leave the session running"
          % (secs, (" (%s)" % label) if label else ""))
    while time.time() < t_end:
        s = sample(base)
        if s:
            frames, dt, nominal = s
            expected = nominal * dt
            ppm = (frames - expected) / expected * 1e6
            deficit_ms = max(0.0, (expected - frames)) / nominal * 1000.0
            rows.append((ppm, deficit_ms, dt, ring(base)))
            sys.stdout.write("    %s %+8.0f ppm\n"
                             % ("!" if deficit_ms > 1.0 else " ", ppm))
            sys.stdout.flush()
    return rows


def report(rows, label):
    if len(rows) < 5:
        raise SystemExit("  too few samples (%d) — is the session live?" % len(rows))
    ppms = [r[0] for r in rows]
    total_wall = sum(r[2] for r in rows)
    total_deficit = sum(r[1] for r in rows)
    # The measurement floor: one sample period of hw_ptr/clock misalignment, ~1ms over the
    # window. Anything inside that band is noise, not lost audio.
    # One millisecond of hw_ptr/clock misalignment spread over the sample window. At a 2.1s
    # window that is ~476ppm, which is exactly the band the readings cluster in.
    floor_ppm = (0.001 / statistics.mean(r[2] for r in rows)) * 1e6
    outliers = [p for p in ppms if p < -2 * floor_ppm]

    print()
    print("  RESULT%s" % ((" — %s" % label) if label else ""))
    print("  " + "-" * 56)
    print("  samples            %d over %.0fs of stream" % (len(rows), total_wall))
    print("  mean               %+.0f ppm     (0 = perfect)" % statistics.mean(ppms))
    print("  spread (sd)        %.0f ppm" % statistics.pstdev(ppms))
    print("  measurement floor  +/-%.0f ppm  (one sample period on this window)" % floor_ppm)
    print()
    r = rows[-1][3] if len(rows[0]) > 3 else None
    if r:
        print("  RING               avail=%s  avail_max=%s  (one period is ~960 frames)"
              % (r.get("avail"), r.get("avail_max")))
        am = r.get("avail_max") or 0
        if am > 2000:
            print("    -> the ring FILLS: the host is outrunning us. A rate mismatch, which")
            print("       the pitch controller is designed to fix.")
        else:
            print("    -> the ring never fills past a period. Frames are missing BEFORE the")
            print("       ring — missed USB transfers, which a feedback loop cannot repair.")
        print()
    # NET rate error, with its uncertainty. Summing only the shortfalls (which this tool
    # used to do, and reported as "audio never captured") is invalid: on a symmetric noisy
    # measurement it returns a large number even when nothing at all is lost. Simulated with
    # zero real loss and our observed sd of 1411ppm, that method reports 0.43 ms/s.
    #
    # Real loss shows up as a MEAN significantly below zero, not as the size of the negative
    # half of the noise.
    mean = statistics.mean(ppms)
    se = statistics.pstdev(ppms) / (len(ppms) ** 0.5)
    print("  NET RATE ERROR     %+.0f ppm  +/- %.0f (standard error)" % (mean, se))
    print("    as audio         %+.3f ms per second of stream" % (mean / 1000.0))
    print("    one-sided sum    %.3f ms/s  <-- what this tool used to report; NOT loss,"
          % (total_deficit / total_wall))
    print("                     it is the negative half of the noise (sd %.0f ppm)"
          % statistics.pstdev(ppms))
    print()
    if mean + 2 * se < -100:
        print("  => The capture IS losing audio: the mean is significantly below zero.")
    elif mean - 2 * se > 100:
        print("  => The capture is receiving MORE than nominal — the host clock runs fast.")
    else:
        print("  => No significant net rate error. Within noise of zero: nothing is being lost")
        print("     at the capture, and the jitter must come from somewhere else.")
    print()
    return total_deficit / total_wall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=int, default=90)
    ap.add_argument("--base", default=CTRL)
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    if not sample(a.base):
        raise SystemExit("  cannot reach the bridge at %s — go live first" % a.base)
    report(run(a.secs, a.base, a.label), a.label)


if __name__ == "__main__":
    main()
