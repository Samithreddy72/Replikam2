#!/usr/bin/env python3
"""NetBridge clock-FFT crackle analyzer  (M4 — walkthrough J4:
"Crackle signature detected", "Bundle ready: ... clock FFT").

Replaces the old journal-grep heuristic in bridge-web.py:clock_suspect() with a
real spectral + time-domain verdict on the return-audio the client sends into the
UAC2 gadget. When the UAC2 sample clock drifts against the host, ALSA repairs the
mismatch by dropping/inserting frames — audible as periodic clicks ("crackle").
That has a signature this looks for:

  * clicks     — impulsive samples far above the local RMS floor (the direct tell)
  * hf_ratio   — energy above 12 kHz over total; clicks are broadband, clean
                 voice/music rolls off, so a raised ratio means "something whitish
                 rides on top of the programme"
  * flatness   — spectral flatness (geo/arith mean of the PSD); clicks whiten an
                 otherwise peaky spectrum
  * xruns      — ALSA under/overrun counter delta, folded in by the sentry (a hard
                 signal — any xrun on an idle stream is real drift, not programme)

Deliberately dependency-free: a hand-rolled radix-2 FFT, no numpy, so it runs on a
stock Raspberry Pi OS python3 with nothing installed. Input is 16-bit PCM (a WAV
written by `arecord`, or raw S16LE on stdin). Output is one JSON line, optionally
an ASCII spectrum for the diagnostics bundle.

    bridge-clock-fft.py capture.wav                 # -> verdict JSON
    bridge-clock-fft.py --ascii capture.wav         # + ASCII spectrum (bundle)
    arecord -D hw:UAC2Gadget -d2 -fS16_LE -r48000 -c2 -t raw | \
        bridge-clock-fft.py --raw --rate 48000 --channels 2
    bridge-clock-fft.py --xruns 3 capture.wav       # fold in an ALSA xrun delta
    bridge-clock-fft.py --selftest                  # synthetic clean vs crackle
"""
import sys
import json
import math
import struct
import wave

FRAME = 4096          # FFT size (power of two — radix-2)
HF_HZ = 12000.0       # "high frequency" boundary for the broadband-click ratio
CLICK_K = 6.0         # a first-difference > CLICK_K * median-slew counts as a click


# --------------------------------------------------------------------------- FFT
def _fft(re, im):
    """In-place iterative radix-2 Cooley-Tukey FFT. len(re) must be a power of 2."""
    n = len(re)
    # bit-reversal permutation
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    # butterflies
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wr_step = math.cos(ang)
        wi_step = math.sin(ang)
        half = length >> 1
        for start in range(0, n, length):
            wr, wi = 1.0, 0.0
            for k in range(half):
                a = start + k
                b = a + half
                tr = wr * re[b] - wi * im[b]
                ti = wr * im[b] + wi * re[b]
                re[b] = re[a] - tr
                im[b] = im[a] - ti
                re[a] += tr
                im[a] += ti
                wr, wi = wr * wr_step - wi * wi_step, wr * wi_step + wi * wr_step
        length <<= 1


def _hann(n):
    if n == 1:
        return [1.0]
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]


# ------------------------------------------------------------------- PCM loading
def load_wav(path):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("only 16-bit PCM supported (got sampwidth=%d)" % sw)
    return _decode_s16(raw, ch), rate


def _decode_s16(raw, ch):
    """Bytes of interleaved S16LE -> mono float list in [-1, 1] (channels averaged)."""
    n = len(raw) // 2
    if n == 0:
        return []
    vals = struct.unpack("<%dh" % n, raw[: n * 2])
    if ch <= 1:
        return [v / 32768.0 for v in vals]
    mono = []
    for i in range(0, n - ch + 1, ch):
        s = 0
        for c in range(ch):
            s += vals[i + c]
        mono.append(s / (ch * 32768.0))
    return mono


# ----------------------------------------------------------------------- metrics
def _welch_psd(x, rate):
    """Hann-windowed, 50%-overlap averaged power spectrum. Returns (psd, freqs)."""
    win = _hann(FRAME)
    wsum = sum(v * v for v in win)  # window power, for normalisation
    half = FRAME // 2
    psd = [0.0] * (half + 1)
    frames = 0
    step = half  # 50% overlap
    pos = 0
    while pos + FRAME <= len(x):
        re = [x[pos + i] * win[i] for i in range(FRAME)]
        im = [0.0] * FRAME
        _fft(re, im)
        for k in range(half + 1):
            psd[k] += (re[k] * re[k] + im[k] * im[k])
        frames += 1
        pos += step
    if frames == 0:
        return None, None
    norm = 1.0 / (frames * wsum * rate)
    psd = [p * norm for p in psd]
    freqs = [k * rate / FRAME for k in range(half + 1)]
    return psd, freqs


def _spectral_flatness(psd):
    # ignore DC and the very lowest bins (rumble/window leakage skews the geo-mean)
    band = [p for p in psd[2:] if p > 0]
    if len(band) < 8:
        return 0.0
    log_sum = sum(math.log(p) for p in band)
    geo = math.exp(log_sum / len(band))
    arith = sum(band) / len(band)
    return geo / arith if arith > 0 else 0.0


def _hf_ratio(psd, freqs):
    total = sum(psd[2:])
    if total <= 0:
        return 0.0
    hf = sum(p for p, f in zip(psd, freqs) if f >= HF_HZ)
    return hf / total


def _click_rate(x, rate):
    """Clicks/sec detected by SLEW, not absolute level. A crackle click is a sudden
    sample-to-sample discontinuity (ALSA dropping/inserting a frame), which shows up
    as a first-difference far above the programme's normal slope — independent of how
    loud the programme is, so a quiet click over a loud tone is still caught. The
    threshold is a robust multiple of the MEDIAN abs-difference (median ignores the
    rare huge clicks themselves), with an absolute floor so silence noise can't blow
    it up. Consecutive over-threshold diffs collapse to one click (a click is one
    event with a rising and a falling edge)."""
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0
    diffs = [abs(x[i] - x[i - 1]) for i in range(1, n)]
    srt = sorted(diffs)
    median = srt[len(srt) // 2]
    thr = max(CLICK_K * median, 0.05)  # floor: real clicks are big, noise slew isn't
    clicks = 0
    prev_over = False
    peak = 0.0
    total_sq = 0.0
    for i, v in enumerate(x):
        total_sq += v * v
        av = v if v >= 0 else -v
        if av > peak:
            peak = av
        over = i > 0 and diffs[i - 1] > thr
        if over and not prev_over:
            clicks += 1
        prev_over = over
    secs = n / rate
    rms_all = math.sqrt(total_sq / n)
    crest_db = 20.0 * math.log10(peak / rms_all) if rms_all > 0 and peak > 0 else 0.0
    return (clicks / secs if secs > 0 else 0.0), crest_db, rms_all


def analyze(x, rate, xruns=0):
    """Return the full metrics + verdict dict for a mono float signal."""
    n = len(x)
    secs = n / rate if rate else 0.0
    click_rate, crest_db, rms = _click_rate(x, rate)
    rms_dbfs = 20.0 * math.log10(rms) if rms > 0 else -120.0
    psd, freqs = _welch_psd(x, rate)
    if psd is None:
        flat = hf = 0.0
        peak_hz = 0.0
    else:
        flat = _spectral_flatness(psd)
        hf = _hf_ratio(psd, freqs)
        # dominant tone (skip DC) — handy context in the bundle
        kmax = max(range(2, len(psd)), key=lambda k: psd[k]) if len(psd) > 2 else 0
        peak_hz = freqs[kmax] if psd else 0.0

    silent = rms_dbfs < -55.0  # nothing to judge; clicks on silence are just noise floor

    # --- score (0-100). Clicks are the primary tell; hf/flatness corroborate;
    #     an ALSA xrun is a hard signal that overrides "the programme looks fine". ---
    score = 0.0
    reasons = []
    if not silent:
        c = min(60.0, click_rate * 12.0)
        if c >= 1:
            reasons.append("clicks=%.1f/s" % click_rate)
        score += c
        if hf > 0.12:
            h = min(25.0, (hf - 0.12) * 300.0)
            score += h
            reasons.append("hf=%.0f%%" % (hf * 100))
        if flat > 0.15:
            fl = min(20.0, (flat - 0.15) * 200.0)
            score += fl
            reasons.append("flat=%.2f" % flat)
    if xruns > 0:
        score += min(45.0, xruns * 15.0)
        reasons.append("xruns=%d" % xruns)

    if score >= 50:
        verdict = "crackle"
    elif score >= 20:
        verdict = "degrading"
    else:
        verdict = "clean"

    return {
        "verdict": verdict,
        "score": round(min(100.0, score), 1),
        "reasons": reasons,
        "seconds": round(secs, 2),
        "rate": rate,
        "silent": silent,
        "click_rate": round(click_rate, 2),
        "crest_db": round(crest_db, 1),
        "rms_dbfs": round(rms_dbfs, 1),
        "hf_ratio": round(hf, 4),
        "flatness": round(flat, 4),
        "peak_hz": round(peak_hz, 1),
        "xruns": xruns,
        "_psd": psd,
        "_freqs": freqs,
    }


def ascii_spectrum(psd, freqs, rows=12, cols=60):
    """A tiny log-frequency / dB spectrum for the diagnostics bundle text file."""
    if not psd:
        return "(no spectrum — capture too short)"
    fmax = freqs[-1]
    # log-spaced column edges from 20 Hz to Nyquist
    lo, hi = math.log10(20.0), math.log10(fmax)
    cell = [0.0] * cols
    for p, f in zip(psd, freqs):
        if f < 20.0:
            continue
        c = int((math.log10(f) - lo) / (hi - lo) * (cols - 1))
        c = max(0, min(cols - 1, c))
        if p > cell[c]:
            cell[c] = p
    peak = max(cell) or 1e-30
    db = [10.0 * math.log10(v / peak) if v > 0 else -99.0 for v in cell]
    lines = []
    for r in range(rows):
        thr = -(r) * (60.0 / rows)  # top row = 0 dB, bottom = -60 dB
        lines.append("".join("#" if d >= thr else " " for d in db))
    out = ["  0dB " + lines[0]]
    out += ["      " + ln for ln in lines[1:-1]]
    out.append(" -60dB " + lines[-1])
    out.append("       20Hz" + " " * (cols - 12) + "%dkHz" % int(fmax / 1000))
    return "\n".join(out)


# ---------------------------------------------------------------------- selftest
def _synth(rate, secs, clicks_per_s=0.0, tone_hz=440.0, noise=0.002):
    """Deterministic synthetic signal: a tone + light noise, optional periodic clicks."""
    n = int(rate * secs)
    # cheap deterministic LCG noise (no random import, reproducible)
    seed = 12345
    x = []
    for i in range(n):
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        nz = (seed / 0x7FFFFFFF - 0.5) * 2.0 * noise
        x.append(0.25 * math.sin(2.0 * math.pi * tone_hz * i / rate) + nz)
    if clicks_per_s > 0:
        period = int(rate / clicks_per_s)
        for i in range(0, n, period):
            x[i] = 0.9 if (i // period) % 2 == 0 else -0.9  # sharp bipolar impulse
    return x


def selftest():
    rate = 48000
    clean = analyze(_synth(rate, 2.0, clicks_per_s=0.0), rate)
    crackle = analyze(_synth(rate, 2.0, clicks_per_s=8.0), rate)
    idle_xrun = analyze(_synth(rate, 2.0, clicks_per_s=0.0, noise=0.0004), rate, xruns=3)
    ok = True
    for name, r, want in (("clean", clean, "clean"),
                          ("crackle", crackle, "crackle"),
                          ("idle+xrun", idle_xrun, ("degrading", "crackle"))):
        got = r["verdict"]
        good = got in (want if isinstance(want, tuple) else (want,))
        ok = ok and good
        print("  %-10s -> %-9s score=%5.1f %s  %s"
              % (name, got, r["score"], r["reasons"], "PASS" if good else "**FAIL**"))
    # FFT sanity: a pure tone must peak at its own frequency (within one bin)
    tone = analyze(_synth(rate, 1.0, tone_hz=3000.0, noise=0.0), rate)
    binhz = rate / FRAME
    peak_ok = abs(tone["peak_hz"] - 3000.0) <= binhz
    ok = ok and peak_ok
    print("  fft-peak   -> %.0f Hz (want 3000, +-%.0f)  %s"
          % (tone["peak_hz"], binhz, "PASS" if peak_ok else "**FAIL**"))
    print("ALL PASS" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main
def main(argv):
    args = argv[1:]
    if "--selftest" in args:
        return selftest()
    ascii_out = "--ascii" in args
    raw = "--raw" in args
    rate = 48000
    channels = 2
    xruns = 0
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--rate":
            rate = int(args[i + 1]); i += 2; continue
        if a == "--channels":
            channels = int(args[i + 1]); i += 2; continue
        if a == "--xruns":
            xruns = int(args[i + 1]); i += 2; continue
        if a in ("--ascii", "--raw"):
            i += 1; continue
        path = a
        i += 1
    if raw:
        data = sys.stdin.buffer.read()
        x = _decode_s16(data, channels)
    elif path:
        try:
            x, rate = load_wav(path)
        except Exception as e:
            # A missing/corrupt capture must not crash with a traceback — the
            # diagnostics bundle folds our stderr in, and the sentry runs us headless.
            print(json.dumps({"verdict": "unknown", "score": 0.0,
                              "error": "%s: %s" % (type(e).__name__, e)}))
            return 2
    else:
        sys.stderr.write("usage: bridge-clock-fft.py [--ascii] <capture.wav>\n"
                         "       ... --raw --rate R --channels C   (S16LE on stdin)\n")
        return 2
    r = analyze(x, rate, xruns=xruns)
    psd, freqs = r.pop("_psd"), r.pop("_freqs")
    print(json.dumps(r))
    if ascii_out:
        print(ascii_spectrum(psd, freqs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
