#!/usr/bin/env python3
"""Capture the WHOLE audio chain at one moment, so a good state and a bad state can be diffed.

WHY THIS EXISTS
---------------
Every attempt to diagnose the jitter so far has been made while it was happening, with no
healthy reference to compare against — so every reading had to be judged on whether it
"looked wrong", which is how a noisy measurement got mistaken for lost audio for an entire
evening. A number is only suspicious next to the same number taken when everything was fine.

So: run this while the audio is GOOD to record a reference. Run it again when the jitter is
back. Then `--compare` shows what actually changed, instead of what merely looks alarming.

    python3 tools/nb-snapshot.py --label good        # while it sounds fine
    python3 tools/nb-snapshot.py --label bad         # when the jitter returns
    python3 tools/nb-snapshot.py --compare good bad

WHAT IT COVERS
--------------
Every point audio passes through, because the fault has already hidden in three of them:

    meeting laptop -> USB -> bridge capture -> encode -> network -> Mac decode -> your ears
                       (1)        (2)           (3)       (4)         (5)          (6)

  1/2  PCM pointers, ring fill distribution, capture rate over many samples
   3   the actual gst command lines running on the bridge, opus settings
   4   mesh path, ping loss and spread, tailscale route
   5   the app's return pipeline, jitter buffer, sink mode
   6   Mac CPU/memory/wifi/coreaudio, UDP socket errors
  all  power, throttling, temperature, interrupt rate, service restarts, config drift

Distributions, not single readings: the interesting metrics are noisy, so it takes many
samples and records mean/sd/min/max. One sample of a noisy quantity tells you nothing, which
is the mistake that cost this project a night.
"""
import argparse, json, os, re, statistics, subprocess, sys, time, urllib.request

CTRL = "http://127.0.0.1:18080"      # the bridge, via the app's mesh helper
APP = "http://127.0.0.1:8765"        # the presenter app
OUT = os.path.expanduser("~/Downloads/netbridge-snapshots")


def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def get(url, timeout=20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def dist(values):
    """Summarise a noisy quantity. A single reading of any of these is worthless."""
    v = [x for x in values if x is not None]
    if not v:
        return None
    return {"n": len(v), "mean": round(statistics.mean(v), 1),
            "sd": round(statistics.pstdev(v), 1) if len(v) > 1 else 0.0,
            "min": min(v), "max": max(v),
            "se": round(statistics.pstdev(v) / (len(v) ** 0.5), 1) if len(v) > 1 else 0.0}


def collect(samples, gap):
    snap = {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "unix": int(time.time())}

    # ---- 5/6: the Mac. Do this first: it is where the audio is actually heard, and it is
    # the side we have historically ignored.
    snap["mac"] = {
        "app": get(APP + "/api/state"),
        "load": sh("uptime"),
        "top_cpu": sh("ps -Ao pcpu,comm -r | head -8"),
        "vm": sh("vm_stat | head -6"),
        "swap": sh("sysctl -n vm.swapusage"),
        "wifi": sh("system_profiler SPAirPortDataType 2>/dev/null | grep -iE 'Signal / Noise|Transmit Rate|Channel:|PHY Mode' | head -5"),
        "iface_errors": sh("netstat -i | awk 'NR==1 || /^en0/'"),
        "udp_stats": sh("netstat -s -p udp | grep -iE 'dropped|bad|received' | head -6"),
        "return_pipeline": sh("ps -Ao args | grep -m1 'rtpjitterbuffer' | cut -c1-400"),
    }

    # PACKET ARRIVAL RATE on this machine. Without BPF we cannot sniff, but the kernel's
    # cumulative UDP counter is enough: sample it twice and the delta is packets per second.
    # At 20ms opus frames the return stream is ~50/s, so a shortfall here localises the loss
    # to the NETWORK rather than to the bridge or the sink.
    def udp_count():
        out = sh("netstat -s -p udp | grep -i 'datagrams received'")
        m = re.search(r"([\d,]+)", out)
        return int(m.group(1).replace(",", "")) if m else None
    u0, t0 = udp_count(), time.monotonic()
    time.sleep(4)
    u1, t1 = udp_count(), time.monotonic()
    snap["mac"]["udp_rx_per_sec"] = round((u1 - u0) / (t1 - t0), 1) if (u0 and u1) else None
    snap["mac"]["audio_out_device"] = sh(
        "system_profiler SPAudioDataType 2>/dev/null | grep -A6 -i 'output' | head -14")
    snap["mac"]["audio_procs"] = sh(
        "ps -Ao pcpu,rss,comm | grep -iE 'coreaudiod|ffmpeg|gst-launch|NetBridgeSource|netbridge-mesh' | grep -v grep")
    snap["mac"]["thermal"] = sh("pmset -g therm 2>/dev/null | head -6")

    # ---- 1/2/3: the bridge.
    st = get(CTRL + "/api/status") or {}
    snap["bridge"] = {
        "status": {k: st.get(k) for k in
                   ("version", "uptime", "temp", "wifi", "speed", "udc", "uac2", "video40",
                    "return_rate", "return_mismatch", "streams", "restarts", "quarantined",
                    "mesh_path", "clock_suspect", "power", "config", "pcm")},
    }

    # ---- 4: the path.
    ip = st.get("tailscale_ip")
    if ip:
        pings = []
        for _ in range(3):
            out = sh("ping -c 20 -i 0.2 -W 2 %s" % ip, timeout=40)
            m = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", out)
            loss = re.search(r"([\d.]+)% packet loss", out)
            if m:
                pings.append({"min": float(m.group(1)), "avg": float(m.group(2)),
                              "max": float(m.group(3)), "mdev": float(m.group(4)),
                              "loss_pct": float(loss.group(1)) if loss else None})
        snap["path"] = {"peer_ip": ip, "runs": pings,
                        "mdev": dist([p["mdev"] for p in pings]),
                        "max": dist([p["max"] for p in pings]),
                        "loss": dist([p["loss_pct"] for p in pings])}

    # ---- the noisy ones: many samples, because one is meaningless.
    ppm, avail, hw_gap, cpu_v, cpu_a = [], [], [], [], []
    print("  sampling the capture %d times (this is the part that must be a distribution)…"
          % samples)
    for i in range(samples):
        ch = get(CTRL + "/api/checks") or {}
        ra = (ch.get("return_audio") or {}).get("detail", "")
        m = re.search(r"(\d+) frames in ([\d.]+)s.*?@ *(\d+)", ra)
        if m:
            f, dt, nom = int(m.group(1)), float(m.group(2)), int(m.group(3))
            ppm.append(round((f - nom * dt) / (nom * dt) * 1e6, 1))
        for key, bucket in (("video_arriving", cpu_v), ("voice_arriving", cpu_a)):
            d = (ch.get(key) or {}).get("detail", "")
            mm = re.search(r"used (\d+) cpu ticks in (\d+)s", d)
            if mm:
                bucket.append(int(mm.group(1)) / (int(mm.group(2)) * 100.0) * 100)
        s2 = get(CTRL + "/api/status") or {}
        p = s2.get("pcm") or {}
        if p.get("avail") is not None:
            avail.append(p["avail"])
        sys.stdout.write("\r    %d/%d" % (i + 1, samples)); sys.stdout.flush()
        time.sleep(gap)
    print()
    # ---- THE WINDOWS HOST, as observed from the bridge.
    #
    # We have no access to the meeting laptop, but the gadget sees it directly and that is
    # enough for the things that matter. The capture rate error IS the comparison of the
    # Windows audio clock against the Pi's: the host decides how many frames to send per
    # second, so a drift there shows up here and nowhere else.
    b2 = get(CTRL + "/api/status") or {}
    snap["windows_host"] = {
        "usb_link": b2.get("speed"),
        "gadget_state": b2.get("udc"),
        "rate_it_selected": b2.get("return_rate"),
        "rate_mismatch_flag": b2.get("return_mismatch"),
        "clock_vs_pi_ppm": dist(ppm),
        "is_streaming_in": (b2.get("streams") or {}).get("return"),
        "note": ("clock_vs_pi_ppm is the Windows audio clock measured against the Pi's. "
                 "Positive means Windows sends more frames per Pi-second than nominal."),
    }
    snap["capture"] = {
        "rate_error_ppm": dist(ppm),
        "ring_avail": dist(avail),
        "feeder_video_cpu_pct": dist(cpu_v),
        "feeder_voice_cpu_pct": dist(cpu_a),
        "raw_ppm": ppm,
    }
    return snap


def deep_bridge(snap, token, dev):
    """Pull a diagnostics bundle for the things no live endpoint exposes: the USB interrupt
    rate, the gadget descriptor the CLIENT negotiated, and per-service CPU. Costs ~40s."""
    import urllib.error
    api = "https://fleet.scine.online"
    def post(t):
        req = urllib.request.Request("%s/admin/devices/%s/commands" % (api, dev),
            data=json.dumps({"type": t, "args": {}}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode()).get("id")
    def bundles():
        req = urllib.request.Request("%s/admin/devices/%s/diagnostics" % (api, dev),
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    try:
        before = (bundles() or [{}])[0].get("id")
        post("diagnose")
        for _ in range(12):
            time.sleep(12)
            b = bundles()
            if b and b[0].get("id") != before:
                bid = b[0]["id"]; break
        else:
            snap["deep"] = {"error": "no bundle appeared"}; return
        import tempfile, tarfile, io
        req = urllib.request.Request("%s/admin/diagnostics/%s" % (api, bid),
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        d = tempfile.mkdtemp()
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(d)
        root = [os.path.join(d, x) for x in os.listdir(d) if x.startswith("diag-")][0]
        def rd(n):
            try:
                return open(os.path.join(root, n), errors="replace").read()
            except Exception:
                return ""
        it = rd("interrupts.txt")
        blocks = it.split("== /proc/interrupts")
        rate = None
        if len(blocks) >= 3:
            def usb_total(b):
                for ln in b.splitlines():
                    if "usb" in ln.lower():
                        n = [int(x) for x in re.findall(r"\b(\d{3,})\b", ln)]
                        if n: return sum(n)
                return None
            a_, b_ = usb_total(blocks[1]), usb_total(blocks[2])
            if a_ and b_: rate = b_ - a_
        snap["deep"] = {
            "usb_interrupts_per_sec": rate,
            "gadget_av": rd("gadget-av.txt")[:1500],
            "usb_av_state": rd("usb-av-state.txt")[:1200],
            "media_health": rd("media-health.txt")[:800],
            "power_txt": rd("power.txt")[:300],
            "gst_cmdlines": [l[:300] for l in re.findall(r"gst-launch-1\.0 [^\n]{0,300}", rd("services.txt"))][:4],
        }
    except Exception as e:
        snap["deep"] = {"error": str(e)[:150]}


def verdict(s):
    """State the few things that actually matter, so a human can read the file."""
    out = []
    c = s.get("capture", {})
    r = c.get("rate_error_ppm")
    if r:
        sig = abs(r["mean"]) > 2 * r["se"]
        out.append("capture rate error %+.0f ppm +/- %.0f  -> %s"
                   % (r["mean"], r["se"], "SIGNIFICANT" if sig else "within noise of zero"))
    a = c.get("ring_avail")
    if a:
        out.append("ring fill mean %.0f max %d  -> %s"
                   % (a["mean"], a["max"],
                      "FILLING (rate mismatch)" if a["max"] > 2000 else "one period, healthy"))
    p = s.get("path", {}).get("mdev")
    l = s.get("path", {}).get("loss")
    if p:
        out.append("path spread mean %.1f ms max %.1f ms, loss %.1f%%"
                   % (p["mean"], p["max"], (l or {}).get("max", 0)))
    b = (s.get("bridge", {}).get("status") or {})
    pw = (b.get("power") or {}).get("rate") or {}
    if pw:
        out.append("brownout %.2f%% of last %ss" % (pw.get("pct", 0), pw.get("samples", 0)))
    w = s.get("windows_host") or {}
    if w:
        out.append("windows host: link %s, selected %s Hz, streaming=%s, mismatch=%s"
                   % (w.get("usb_link"), w.get("rate_it_selected"),
                      w.get("is_streaming_in"), w.get("rate_mismatch_flag")))
    app = s.get("mac", {}).get("app") or {}
    mc = s.get("mac", {})
    out.append("presenter: udp rx %.1f pkt/s (return stream is ~50/s)" % (mc.get("udp_rx_per_sec") or 0))
    dp = s.get("deep") or {}
    if dp.get("usb_interrupts_per_sec") is not None:
        out.append("pi: %d USB interrupts/s (SOF storm would be 250000+)" % dp["usb_interrupts_per_sec"])
    out.append("mac jitter buffer %s ms, sink_sync=%s, conceal=%s"
               % (app.get("return_jitter_ms"), app.get("return_sink_sync"), app.get("return_conceal")))
    return out


def compare(a, b):
    print("\n  %-34s %-24s %-24s" % ("metric", "A", "B"))
    print("  " + "-" * 84)

    def row(label, x, y, fmt="%s"):
        mark = "" if x == y else "   <-- CHANGED"
        print("  %-34s %-24s %-24s%s" % (label, fmt % x if x is not None else "-",
                                         fmt % y if y is not None else "-", mark))

    for key, label in (("rate_error_ppm", "capture rate error (mean ppm)"),
                       ("ring_avail", "ring fill (mean)"),
                       ("feeder_video_cpu_pct", "video feeder CPU %"),
                       ("feeder_voice_cpu_pct", "voice feeder CPU %")):
        xa = (a.get("capture", {}).get(key) or {}); xb = (b.get("capture", {}).get(key) or {})
        row(label, xa.get("mean"), xb.get("mean"))
        if key == "rate_error_ppm":
            row("  its spread (sd)", xa.get("sd"), xb.get("sd"))
        if key == "ring_avail":
            row("  its max", xa.get("max"), xb.get("max"))

    for key, label in (("mdev", "path spread mean (ms)"), ("max", "path max (ms)"),
                       ("loss", "packet loss %")):
        xa = (a.get("path", {}).get(key) or {}); xb = (b.get("path", {}).get(key) or {})
        row(label, xa.get("mean"), xb.get("mean"))

    sa = a.get("bridge", {}).get("status", {}); sb = b.get("bridge", {}).get("status", {})
    for k in ("temp", "wifi", "return_rate", "return_mismatch", "udc", "speed",
              "clock_suspect", "restarts", "quarantined"):
        row("bridge %s" % k, sa.get(k), sb.get(k))
    row("brownout %", ((sa.get("power") or {}).get("rate") or {}).get("pct"),
                      ((sb.get("power") or {}).get("rate") or {}).get("pct"))
    row("config state", (sa.get("config") or {}).get("state"), (sb.get("config") or {}).get("state"))

    wa = a.get("windows_host") or {}; wb = b.get("windows_host") or {}
    for k in ("usb_link", "gadget_state", "rate_it_selected", "rate_mismatch_flag", "is_streaming_in"):
        row("windows %s" % k, wa.get(k), wb.get(k))
    row("windows clock vs pi (ppm)", (wa.get("clock_vs_pi_ppm") or {}).get("mean"),
                                     (wb.get("clock_vs_pi_ppm") or {}).get("mean"))
    row("mac udp rx pkt/s", a.get("mac", {}).get("udp_rx_per_sec"), b.get("mac", {}).get("udp_rx_per_sec"))
    row("pi usb interrupts/s", (a.get("deep") or {}).get("usb_interrupts_per_sec"),
                               (b.get("deep") or {}).get("usb_interrupts_per_sec"))

    aa = a.get("mac", {}).get("app") or {}; ab = b.get("mac", {}).get("app") or {}
    for k in ("version", "return_jitter_ms", "return_sink_sync", "return_conceal", "return_gain"):
        row("mac app %s" % k, aa.get(k), ab.get(k))
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", help="name this snapshot (e.g. good, bad, after-fix)")
    ap.add_argument("--samples", type=int, default=25)
    ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--deep", action="store_true",
                    help="also pull a diagnostics bundle (interrupt rate, gadget descriptor); ~60s")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    if a.compare:
        def load(n):
            p = n if os.path.exists(n) else os.path.join(OUT, "%s.json" % n)
            with open(p) as f:
                return json.load(f)
        compare(load(a.compare[0]), load(a.compare[1]))
        return 0

    if not a.label:
        ap.error("--label is required (e.g. --label good)")
    if not get(CTRL + "/api/status"):
        raise SystemExit("  cannot reach the bridge — go live first")
    s = collect(a.samples, a.gap)
    if a.deep:
        try:
            tok = json.load(open(os.path.expanduser("~/.netbridge-source/state.json")))["token"]
            print("  pulling a diagnostics bundle for the deep metrics…")
            deep_bridge(s, tok, "100000005d5ade42")
        except Exception as e:
            s["deep"] = {"error": str(e)[:150]}
    s["label"] = a.label
    path = os.path.join(OUT, "%s.json" % a.label)
    with open(path, "w") as f:
        json.dump(s, f, indent=2, sort_keys=True)
    print("\n  SNAPSHOT '%s'" % a.label)
    print("  " + "-" * 60)
    for line in verdict(s):
        print("    " + line)
    print("\n  saved: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
