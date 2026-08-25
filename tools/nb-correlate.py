#!/usr/bin/env python3
"""Record every source on ONE timeline, so the next jitter episode can be correlated.

WHY THIS EXISTS
---------------
Every instrument this project has produces a good answer to its own question, and none of them
share a clock. nb-snapshot takes a portrait at one moment. rtp-probe watches sequence numbers
for a window. return-rate-probe counts silence in a capture. The flight recorder samples the
Pi once a second. When jitter happens you end up with five files, five time bases, and no way
to say "the buffer emptied 400ms AFTER the brownout started" - which is the only sentence that
would settle the cause.

So this samples all of them together, on one monotonic clock, at a fixed cadence, and writes
one row per tick. Correlation becomes arithmetic instead of guesswork.

It is a RECORDER. It changes nothing, tunes nothing, and reaches the bridge only through
read-only endpoints, so it is safe to leave running during a real meeting.

    python3 tools/nb-correlate.py --bridge 192.168.1.11 --secs 300 --label jitter-episode
    python3 tools/nb-correlate.py --analyse runs/jitter-episode.jsonl

WHAT TO DO WITH IT
------------------
Start it when a session begins. If the audio breaks, note roughly when. Then --analyse prints
what moved in the seconds around each disturbance and, crucially, what did NOT - which is how
a suspect gets eliminated rather than accumulated.

The evidence standard this exists to serve: the Wi-Fi theory was retired because two clean
sessions ran at WORSE signal than a jittery one. Only a timeline can show that.
"""
import argparse, json, os, pathlib, re, subprocess, sys, time, urllib.request

APP = "http://127.0.0.1:8765"
OUT = pathlib.Path.home() / "Downloads" / "netbridge-runs"


def sh(cmd, timeout=4):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        # Bridge responses can carry raw newlines inside JSON strings; strict parsing rejects
        # a payload that is otherwise perfectly usable.
        return json.loads(re.sub(r"[\x00-\x1f]", " ", raw), strict=False)
    except Exception:
        return None


def udp_received():
    """Cumulative UDP datagrams on this Mac. The DELTA between ticks is the arrival rate, and
    a shortfall localises loss to the network rather than the bridge or the sink."""
    m = re.search(r"([\d,]+)", sh("netstat -s -p udp | grep -i 'datagrams received'"))
    return int(m.group(1).replace(",", "")) if m else None


def udp_dropped():
    m = re.search(r"([\d,]+)", sh("netstat -s -p udp | grep -i 'dropped due to full socket'"))
    return int(m.group(1).replace(",", "")) if m else None


def sample(bridge, prev):
    """One row. Everything here is a read; nothing is changed anywhere."""
    t = time.monotonic()
    row = {"t": round(t, 3), "wall": time.strftime("%H:%M:%S")}

    st = get("http://%s:8080/api/status" % bridge) or {}
    pcm = st.get("pcm") or {}
    pw = st.get("power") or {}
    usb = st.get("usb") or {}
    row.update({
        # --- the bridge
        "wifi": _int(st.get("wifi")),
        "temp": _float(str(st.get("temp") or "").rstrip("'C")),
        "udc": st.get("udc"),
        "usb_diag": usb.get("diagnosis"),
        "return_rate": st.get("return_rate"),
        "return_mismatch": st.get("return_mismatch"),
        "streams": st.get("streams"),
        # sticky-since-boot, which is why it is useless alone: it reads 0x50000 in sessions
        # with perfectly clean audio. The LIVE flag and the rate are the discriminating ones.
        "throttled": st.get("throttled"),
        "brownout_live": pw.get("live"),
        "brownout_pct": (pw.get("rate") or {}).get("pct"),
        # --- the capture ring: hw_ptr is what the host delivered, appl_ptr what we consumed
        "pcm_state": pcm.get("state"),
        "pcm_avail": pcm.get("avail"),
        "pcm_avail_max": pcm.get("avail_max"),
        "hw_ptr": pcm.get("hw_ptr"),
        "appl_ptr": pcm.get("appl_ptr"),
        "restarts": st.get("restarts"),
        "clock_suspect": st.get("clock_suspect"),
        "mesh_via": (st.get("mesh_path") or {}).get("via"),
    })

    ch = get("%s/api/checks?host=%s" % (APP, bridge)) or {}
    for k in ("video_arriving", "voice_arriving", "return_audio"):
        v = ch.get(k) or {}
        row[k] = bool(v.get("ok"))
    # feeder CPU appears in the check detail; it is the cheapest proxy for "is it doing work"
    for k, out in (("video_arriving", "video_cpu_ticks"), ("voice_arriving", "voice_cpu_ticks")):
        m = re.search(r"used (\d+) cpu ticks in (\d+)s", ((ch.get(k) or {}).get("detail") or ""))
        row[out] = round(int(m.group(1)) / int(m.group(2)), 1) if m else None

    stt = get("%s/api/state" % APP) or {}
    row.update({"app_live": stt.get("live"),
                "jitter_ms": _int(stt.get("return_jitter_ms")),
                "fec": stt.get("return_fec"), "plc": stt.get("return_plc"),
                "legs_ok": (stt.get("legs") or {}).get("ok"),
                "legs_drops": (stt.get("legs") or {}).get("drops")})

    u, d = udp_received(), udp_dropped()
    row["udp_total"] = u
    row["udp_drop_total"] = d
    if prev and prev.get("udp_total") and u:
        gap = t - prev["t"]
        row["udp_per_s"] = round((u - prev["udp_total"]) / gap, 1) if gap > 0 else None
        row["udp_dropped_delta"] = (d - prev["udp_drop_total"]) if (d and prev.get("udp_drop_total")) else None
    # capture progress between ticks: the honest measure of whether audio kept flowing
    if prev and prev.get("hw_ptr") and row.get("hw_ptr"):
        gap = t - prev["t"]
        frames = row["hw_ptr"] - prev["hw_ptr"]
        nominal = row.get("return_rate") or 48000
        # The pointer is read at the END of a sample that itself takes time, so the true
        # interval is uncertain by however long the two reads took. Record the gap alongside
        # the rate so an implausible value can be recognised as a timing artifact rather than
        # being mistaken for a real clock error - this project has already lost an evening to
        # exactly that confusion.
        row["gap_s"] = round(gap, 3)
        if gap > 0.5 and nominal:
            row["capture_fps"] = round(frames / gap, 1)
            row["capture_ppm"] = round((frames / gap / nominal - 1) * 1e6)
    return row


def _int(v):
    try: return int(str(v).strip())
    except Exception: return None


def _float(v):
    try: return float(str(v).strip())
    except Exception: return None


def record(bridge, secs, every, label):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / ("%s.jsonl" % label)
    print("  recording %ss every %ss -> %s" % (secs, every, path))
    print("  (safe to leave running during a meeting: every source below is read-only)")
    prev, n = None, 0
    t0 = time.monotonic()
    # Absolute schedule, not "sleep for the leftover". Sampling takes a variable amount of
    # time - the bridge is on Wi-Fi and a slow reply can cost a second - so subtracting the
    # work from a fixed interval drifts, and drift is fatal here: every derived rate divides
    # by the gap between ticks, so an irregular cadence turns a healthy capture into an
    # apparent 18000ppm error. Ask for tick k at t0 + k*every and skip a tick if we are late.
    next_at = t0
    with open(path, "w") as f:
        while time.monotonic() - t0 < secs:
            row = sample(bridge, prev)
            f.write(json.dumps(row) + "\n"); f.flush()
            prev, n = row, n + 1
            sys.stdout.write("\r    %d rows  wifi=%s brownout=%s capture=%s ppm  " % (
                n, row.get("wifi"), row.get("brownout_live"), row.get("capture_ppm")))
            sys.stdout.flush()
            next_at += every
            now = time.monotonic()
            if next_at < now:                 # fell behind: resync rather than sprint to catch up
                next_at = now
            time.sleep(max(0.0, next_at - now))
    print("\n  saved %d rows: %s" % (n, path))
    return path


def analyse(path):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    if len(rows) < 3:
        print("  too few rows to say anything"); return 1
    print("\n  %d rows over %.0fs" % (len(rows), rows[-1]["t"] - rows[0]["t"]))

    def col(k):
        return [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]

    print("\n  ---- what moved, and what did not ----")
    print("  %-18s %-10s %-10s %-10s %s" % ("metric", "min", "max", "mean", "verdict"))
    for k, moved_note in (("wifi", "signal"), ("temp", "thermal"),
                          ("brownout_pct", "power"), ("capture_ppm", "capture clock"),
                          ("udp_per_s", "arrival rate"), ("video_cpu_ticks", "video work"),
                          ("voice_cpu_ticks", "voice work"), ("jitter_ms", "buffer depth")):
        v = col(k)
        if not v:
            print("  %-18s %s" % (k, "no data")); continue
        lo, hi = min(v), max(v)
        mean = sum(v) / len(v)
        spread = hi - lo
        # "did not move" is the useful half. A metric that is flat through a disturbance is a
        # suspect ELIMINATED, and eliminations are what this project has been short of.
        flat = spread <= (abs(mean) * 0.05 if mean else 0.5)
        print("  %-18s %-10.1f %-10.1f %-10.1f %s" % (k, lo, hi, mean,
              "FLAT - eliminated" if flat else "moved (%s)" % moved_note))

    # Per-tick capture rate is too noisy to read directly: hw_ptr is sampled at whatever moment
    # the bridge answered, so a 200ms variation in HTTP response time over a 3s tick shows up
    # as ~60000ppm of apparent clock error. Over the whole run that timing noise averages out,
    # and THAT is the number worth quoting. Reporting the per-tick swing as a clock error would
    # repeat exactly the mistake that cost this project an evening.
    hp = [(r["t"], r["hw_ptr"]) for r in rows if isinstance(r.get("hw_ptr"), int)]
    if len(hp) >= 3:
        span = hp[-1][0] - hp[0][0]
        frames = hp[-1][1] - hp[0][1]
        nominal = next((r.get("return_rate") for r in rows if r.get("return_rate")), 48000)
        if span > 5 and nominal:
            ppm = (frames / span / nominal - 1) * 1e6
            per = [r["capture_ppm"] for r in rows if isinstance(r.get("capture_ppm"), (int, float))]
            swing = (max(per) - min(per)) if per else 0
            print("\n  ---- capture clock over the WHOLE run ----")
            print("  %+.0f ppm over %.0fs  (per-tick readings swing %.0f ppm; that spread is"
                  % (ppm, span, swing))
            print("   sampling jitter, not clock error — only this run-level figure is meaningful)")

    print("\n  ---- disturbances ----")
    bad = [r for r in rows if r.get("brownout_live") or r.get("legs_drops")
           or r.get("udp_dropped_delta") or r.get("video_arriving") is False
           or r.get("voice_arriving") is False or r.get("return_audio") is False]
    if not bad:
        print("  none recorded — nothing failed during this window")
    else:
        for r in bad[:20]:
            why = []
            if r.get("brownout_live"): why.append("BROWNOUT")
            if r.get("udp_dropped_delta"): why.append("udp drops=%s" % r["udp_dropped_delta"])
            if r.get("legs_drops"): why.append("leg drops=%s" % r["legs_drops"])
            for k in ("video_arriving", "voice_arriving", "return_audio"):
                if r.get(k) is False: why.append("%s DOWN" % k)
            print("  %s  %s   wifi=%s capture=%sppm udp=%s/s" % (
                r.get("wall"), ", ".join(why), r.get("wifi"),
                r.get("capture_ppm"), r.get("udp_per_s")))
        if len(bad) > 20:
            print("  ... and %d more" % (len(bad) - 20))
    print("\n  Correlate by TIME: a cause precedes its effect. A metric that is flat across a")
    print("  disturbance is eliminated, which is worth more than another suspect.\n")
    return 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--bridge", default="192.168.1.11")
    ap.add_argument("--secs", type=int, default=300)
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--label", default="run")
    ap.add_argument("--analyse", default=None, help="analyse an existing .jsonl instead")
    a = ap.parse_args()
    if a.analyse:
        return analyse(a.analyse)
    return analyse(record(a.bridge, a.secs, a.every, a.label))


if __name__ == "__main__":
    sys.exit(main())
