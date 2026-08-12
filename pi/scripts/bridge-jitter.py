#!/usr/bin/env python3
"""Jitter: find the culprit, then apply the smallest thing that fixes it.

WHY THIS EXISTS
---------------
"I can hear jitter" had no action behind it. The fleet menu offered `profile:wan`, which
(a) was already applied, and (b) tunes the WRONG DIRECTION — the bridge's rtpjitterbuffer
carries the presenter's voice toward the room, while the audio the operator is complaining
about is decoded on their own laptop through the app's buffer. Clicking it cost a five-second
video freeze and changed nothing they could hear.

The other menu entry that mentions audio, `reset-clock`, rebuilds the USB gadget: the
meeting laptop's camera, microphone and speakers all vanish and have to be re-selected
mid-call. That is the correct fix for a degraded UAC2 clock and a disaster for anything else.

So the useful thing is not another button. It is knowing WHICH of this system's known
culprits is responsible, because each has a different fix and most of the fixes are harmful
when applied to the wrong cause.

THE CULPRITS, ALL OBSERVED ON THIS HARDWARE
-------------------------------------------
  under-voltage      0x50000 sticky, 327 live episodes in one flight recording. The SoC
                     throttles, so packets arrive LATE, NOT LOST — 0% loss with audible
                     stutter. No software fix exists; buffer depth masks it.
  network bursts     Wi-Fi interference. Also lateness, so the same treatment works.
  rate mismatch      pipeline opened at one rate while the host moved to another. Alive,
                     errorless, and audibly robotic at 72% speed.
  concealment on     a 2026-08-03 regression: opusdec concealment invents audio across gaps
                     and it sounds like jitter. Cost days of chasing the network.
  resampler clicks   only at followed rates (44.1k/32k), never at 48k pass-through.
  stale peer         the presenter's mesh node takes a new IP on every app start.
  relayed path       roughly doubles latency versus a direct mesh path.
  config drift       something changed since the state that was verified good.
  degraded clock     genuinely the UAC2 clock — the one case reset-clock is for.

Usage:
  bridge-jitter.py diagnose [--json]        measure, name the culprit, recommend one action
  bridge-jitter.py fix --rung N [--json]    apply a ladder rung (1 = no video interruption)
  bridge-jitter.py reset [--json]           clear fleet tuning, hand control back
"""
import argparse, glob, importlib.util, json, os, re, subprocess, sys, time

PRESENTER_TUNE = "/data/presenter-tuning.json"
PEER_DEF = "/etc/default/bridge-return-audio"
RUNDIR = "/run/bridge-return-audio"
WEB_PY = "/usr/local/bin/bridge-web.py"
GOLDEN_PY = "/usr/local/bin/bridge-golden.py"

# The ladder. Rung 1 is the default because it is the only one that does not interrupt video,
# and because buffer depth is the universal treatment for every LATENESS cause — which is
# most of them. Rungs are cumulative in effect, not in damage: each is tried alone.
RUNGS = {
    1: {"jitter_ms": 400, "what": "raise the presenter's return buffer to 400ms",
        "cost": "about a second of room audio; video untouched"},
    2: {"jitter_ms": 600, "what": "raise the presenter's return buffer to 600ms",
        "cost": "about a second of room audio; video untouched; you hear the room later"},
    3: {"jitter_ms": 800, "profile": "wan",
        "what": "800ms buffer AND the bridge's WAN jitter profile",
        "cost": "~5s video freeze — the bridge restarts its feeders"},
}


def _load(path, name):
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


def _sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def _peer():
    try:
        with open(PEER_DEF) as f:
            m = re.search(r"^\s*RETURN_DEST_IP\s*=\s*\"?([0-9.]+)", f.read(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def measure_path(ip, count=20):
    """Loss and jitter to the presenter. Named mdev by ping; it is the spread that matters,
    not the average — a steady 200ms link sounds fine, a 20ms one with 60ms spikes does not."""
    if not ip:
        return None
    out = _sh("ping -c %d -i 0.2 -W 2 %s 2>/dev/null" % (count, ip), timeout=count + 15)
    loss = re.search(r"([0-9.]+)% packet loss", out)
    stats = re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)\s*ms", out)
    if not stats:
        return {"reachable": False}
    return {"reachable": True,
            "loss_pct": float(loss.group(1)) if loss else None,
            "min_ms": float(stats.group(1)), "avg_ms": float(stats.group(2)),
            "max_ms": float(stats.group(3)), "mdev_ms": float(stats.group(4))}


def diagnose():
    """Measure everything cheap, then name the culprit."""
    web = _load(WEB_PY, "bridge_web")
    peer = _peer()
    ev = {}

    # --- power. The one cause with no software fix, so it is checked first: if the board is
    # browning out, every other reading is downstream of that.
    ev["power"] = web.power_state() if web else None

    # --- the path to the presenter.
    ev["path"] = measure_path(peer)

    # --- rate mismatch. The follower publishes this file when the device and pipeline
    # disagree; it self-clears on the next good start.
    mm = None
    try:
        with open(os.path.join(RUNDIR, "mismatch")) as f:
            mm = json.load(f)
    except Exception:
        pass
    ev["rate_mismatch"] = mm
    try:
        with open(os.path.join(RUNDIR, "rate")) as f:
            ev["pipeline_rate"] = f.read().strip()
    except Exception:
        ev["pipeline_rate"] = None

    # --- the running return pipeline, read from the process table rather than from source:
    # what matters is what is EXECUTING, which an override can change without changing git.
    cmd = ""
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(p, "rb") as f:
                c = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            if "opusenc" in c and "alsasrc" in c:
                cmd = c
                break
        except Exception:
            continue
    ev["return_cmdline_found"] = bool(cmd)
    # The 2026-08-03 regression. `plc=true` is correct and wanted; an explicit concealment
    # setting is the thing that made clean audio sound broken.
    ev["concealment"] = bool(re.search(r"conceal(ment)?=(true|1)", cmd))
    ev["resampling"] = bool(cmd and ev["pipeline_rate"] not in (None, "", "48000"))

    # --- mesh path and config drift, both cheap and both real causes.
    st = {}
    try:
        st = json.loads(_sh("curl -s --max-time 4 http://127.0.0.1:8080/api/status") or "{}")
    except Exception:
        pass
    ev["mesh_path"] = (st.get("mesh_path") or {}).get("via")
    gold = _load(GOLDEN_PY, "bridge_golden")
    ev["config"] = gold.diff() if gold else None
    ev["peer"] = peer
    ev["fleet_tuning"] = _read_tuning()

    return {"evidence": ev, "findings": _rank(ev), "ts": int(time.time())}


def _rank(ev):
    """Culprits, most-likely first, each with the ONE action that treats it.

    `action: None` is deliberate and important — several real causes have no fleet button,
    and offering one anyway is how an operator ends up rebuilding a USB gadget mid-call to
    fix a Wi-Fi problem.
    """
    f = []
    p = ev.get("power") or {}
    rate = (p.get("rate") or {})
    pct = rate.get("pct")

    if pct is not None and pct >= 2.0:
        f.append({"culprit": "under-voltage",
                  "confidence": "high",
                  "detail": "browning out %.2f%% of the last %ds. The SoC throttles, so packets "
                            "arrive LATE, not lost — this sounds exactly like a network fault "
                            "and no network change will fix it."
                            % (pct, rate.get("samples", 0)),
                  "action": "jitter-fix", "rung": 2,
                  "note": "No software fix exists. Buffer depth masks it; the cure is electrical."})
    elif p.get("ever") and pct is not None and pct > 0:
        f.append({"culprit": "under-voltage (intermittent)",
                  "confidence": "medium",
                  "detail": "has browned out since boot; currently %.2f%% of the last %ds. "
                            "Below ~1%% this board has been confirmed clean by ear."
                            % (pct, rate.get("samples", 0)),
                  "action": "jitter-fix", "rung": 1,
                  "note": "Watch it. If the rate climbs above 2%, expect audible stutter."})

    if ev.get("rate_mismatch"):
        mm = ev["rate_mismatch"]
        f.append({"culprit": "sample-rate mismatch",
                  "confidence": "high",
                  "detail": "device at %sHz, pipeline at %sHz. A mismatched-but-alive pipeline "
                            "throws no error and sounds robotic — 72%% speed in the observed case."
                            % (mm.get("device_rate"), mm.get("pipeline_rate")),
                  "action": "restart", "rung": None,
                  "note": "The mismatch watchdog normally self-heals within ~10s. If this "
                          "persists, the follower is stuck."})

    if ev.get("concealment"):
        f.append({"culprit": "opus concealment is ON",
                  "confidence": "high",
                  "detail": "the running pipeline has concealment enabled. It invents audio "
                            "across gaps, which sounds like jitter. This exact regression cost "
                            "days of network debugging on 2026-08-03.",
                  "action": "deploy-script", "rung": None,
                  "note": "A code fix, not a tuning change: deploy the corrected "
                          "bridge-return-audio.sh."})

    path = ev.get("path") or {}
    if path.get("reachable") is False:
        f.append({"culprit": "presenter unreachable",
                  "confidence": "high",
                  "detail": "the bridge cannot ping the presenter at all — this is not jitter, "
                            "the return path is down.",
                  "action": "mesh-key", "rung": None, "note": None})
    elif path.get("mdev_ms") is not None:
        mdev, loss = path["mdev_ms"], path.get("loss_pct") or 0.0
        if loss >= 2.0:
            f.append({"culprit": "packet loss",
                      "confidence": "high",
                      "detail": "%.1f%% loss to the presenter. Loss is not lateness: a bigger "
                                "buffer cannot replace packets that never arrive." % loss,
                      "action": None, "rung": None,
                      "note": "Move the radio or change channel. FEC already runs at 20%."})
        elif mdev >= 8.0:
            f.append({"culprit": "network bursts",
                      "confidence": "high" if mdev >= 15 else "medium",
                      "detail": "path spread %.1fms (max %.1fms) with %.1f%% loss. Lateness, "
                                "not loss — buffer depth absorbs it."
                                % (mdev, path.get("max_ms", 0), loss),
                      "action": "jitter-fix", "rung": 1 if mdev < 15 else 2, "note": None})

    if ev.get("mesh_path") == "relay":
        f.append({"culprit": "relayed mesh path",
                  "confidence": "medium",
                  "detail": "media is going through a relay instead of a direct path, which "
                            "roughly doubles latency and adds variance.",
                  "action": "mesh-key", "rung": None, "note": None})

    if ev.get("resampling"):
        f.append({"culprit": "resampler active (followed rate)",
                  "confidence": "low",
                  "detail": "the pipeline is at %sHz, so audioresample is doing real work. "
                            "Clicks have been heard at followed rates and never at 48k "
                            "pass-through." % ev.get("pipeline_rate"),
                  "action": None, "rung": None,
                  "note": "If the meeting app allows it, 48000 Hz is the pass-through case."})

    cfg = ev.get("config") or {}
    if cfg.get("saved") and cfg.get("drift"):
        f.append({"culprit": "config drift",
                  "confidence": "medium",
                  "detail": "%d field(s) differ from the known-good baseline (%d restorable "
                            "from here)." % (len(cfg["drift"]), cfg.get("restorable_count", 0)),
                  "action": "golden-restore" if cfg.get("restorable_count") else None,
                  "rung": None,
                  "note": None if cfg.get("restorable_count") else
                          "The drift is in code or the image — needs a deploy or reflash."})

    if not f:
        f.append({"culprit": "nothing measurable",
                  "confidence": "n/a",
                  "detail": "power clean, path steady, rates matched, config known-good. "
                            "If jitter is audible anyway, raise the buffer and say so — every "
                            "instrument on this system once read clean while it was audible.",
                  "action": "jitter-fix", "rung": 1, "note": None})
    return f


def _read_tuning():
    try:
        with open(PRESENTER_TUNE) as f:
            return json.load(f)
    except Exception:
        return None


def _write_tuning(d):
    tmp = PRESENTER_TUNE + ".tmp"
    os.makedirs(os.path.dirname(PRESENTER_TUNE), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PRESENTER_TUNE)
    os.chmod(PRESENTER_TUNE, 0o644)      # bridge-web serves it as 'pi'


def fix(rung):
    if rung not in RUNGS:
        return {"ok": False, "error": "rung must be one of %s" % sorted(RUNGS)}
    r = RUNGS[rung]
    _write_tuning({"jitter_ms": r["jitter_ms"], "rung": rung, "ts": int(time.time()),
                   "reason": "jitter fix rung %d" % rung})
    out = {"ok": True, "rung": rung, "what": r["what"], "cost": r["cost"],
           "applies_in": "up to 10s — the presenter app picks this up on its next poll",
           "video_interrupted": False}
    if r.get("profile"):
        subprocess.run(["/usr/local/bin/bridge", "profile", r["profile"]],
                       capture_output=True)
        out["video_interrupted"] = True
        out["also"] = "bridge jitter profile set to %s" % r["profile"]
    return out


def reset():
    existed = os.path.exists(PRESENTER_TUNE)
    # Publish an explicit "back to default" rather than deleting the file. A vanished file is
    # indistinguishable from one that was never written, so the app would keep whatever it
    # last applied and the reset would silently not happen.
    _write_tuning({"jitter_ms": 250, "rung": 0, "ts": int(time.time()),
                   "reason": "reset to default"})
    return {"ok": True, "had_override": existed, "jitter_ms": 250,
            "applies_in": "up to 10s",
            "note": "presenter buffer returns to its 250ms default"}


def _print_diag(d):
    ev = d["evidence"]
    print("  measured:")
    p = ev.get("power") or {}
    rate = p.get("rate") or {}
    print("    power        %s%s" % (p.get("summary", "?"),
          ("  [%.2f%% of last %ds]" % (rate["pct"], rate.get("samples", 0))) if rate.get("pct") is not None else ""))
    path = ev.get("path") or {}
    if path.get("reachable"):
        print("    path         avg %.1fms  spread %.1fms  max %.1fms  loss %.1f%%"
              % (path["avg_ms"], path["mdev_ms"], path["max_ms"], path.get("loss_pct") or 0))
    else:
        print("    path         unreachable" if path else "    path         not measured")
    print("    rate         pipeline %s%s" % (ev.get("pipeline_rate"),
          "  MISMATCH" if ev.get("rate_mismatch") else ""))
    print("    mesh         %s" % (ev.get("mesh_path") or "?"))
    cfg = ev.get("config") or {}
    print("    config       %s" % cfg.get("state", "?"))
    print("\n  most likely cause first:")
    for i, f in enumerate(d["findings"], 1):
        print("    %d. %s  (%s confidence)" % (i, f["culprit"], f["confidence"]))
        print("       %s" % f["detail"])
        if f.get("action"):
            print("       -> fleet action: %s%s" % (f["action"],
                  (" (rung %s)" % f["rung"]) if f.get("rung") else ""))
        else:
            print("       -> no fleet action can fix this")
        if f.get("note"):
            print("       %s" % f["note"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("diagnose", "fix", "reset"))
    ap.add_argument("--rung", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.action == "diagnose":
        d = diagnose()
        print(json.dumps(d, indent=2)) if a.json else _print_diag(d)
    elif a.action == "fix":
        r = fix(a.rung)
        print(json.dumps(r, indent=2)) if a.json else print("  %s" % (r.get("what") or r.get("error")))
        return 0 if r.get("ok") else 1
    else:
        r = reset()
        print(json.dumps(r, indent=2)) if a.json else print("  %s" % r["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
