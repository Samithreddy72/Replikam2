#!/usr/bin/env python3
"""Golden Profile — record a known-good configuration, and see what has drifted from it.

WHY
---
When audio goes wrong the first question is always "what changed?", and until now there was
no way to answer it. The bridge's behaviour is spread across four config files, a gadget
descriptor written at boot, and whatever signed override happens to be installed — so
"it was fine yesterday" was an assertion nobody could check.

This records the state that was verified good, and diffs against it on demand.

WHAT IT CANNOT DO, DELIBERATELY
-------------------------------
Not everything that drifts can be un-drifted from here, and pretending otherwise would be
worse than useless — an operator clicking "restore" during a call needs to know whether
that will actually fix anything.

  RESTORABLE   runtime tunables: the jitter profile, the return-audio tune file, AEC.
               Written to /etc/default/*, applied by restarting one or two services.

  REPORT-ONLY  the gadget's advertised sample rates and the code identity. c_srate is
               written at every boot by /home/pi/uvc-raw-setup.sh, which lives on the
               READ-ONLY root — poking configfs now would be undone by the next reboot and
               would give a false sense that the problem was fixed. If those drift, the
               IMAGE or a signed override is different, and the fix is deploy-script or a
               reflash. So they are recorded as a fingerprint and reported, never "restored".

Usage:
  bridge-golden.py save    [--note TEXT]   snapshot the current state as known-good
  bridge-golden.py show    [--json]        print the stored profile
  bridge-golden.py diff    [--json]        compare current state against it
  bridge-golden.py restore [--json]        put the RESTORABLE fields back
"""
import argparse, glob, hashlib, json, os, re, subprocess, sys, time

GOLDEN = "/data/golden-profile.json"
# The FACTORY baseline, written into the image at build time and living on the read-only
# root, so it survives a /data wipe.
#
# WHY IT EXISTS: a freshly flashed card had no baseline at all. The Config column read
# "no baseline" and "restore known-good" had nothing to restore TO — on the very card where
# drift is most likely, because everything on it is new. Now every bridge has a floor from
# first boot: the configuration that was verified by ear on this hardware.
#
# Precedence: an operator's own save always wins. The factory copy is a floor, not a ceiling.
FACTORY = "/etc/bridge/golden-default.json"
NET_DEF = "/etc/default/bridge-net"
TUNE_DEF = "/etc/default/bridge-return-tune"
PEER_DEF = "/etc/default/bridge-return-audio"

# Scripts whose content defines how the media behaves. A change here is a code change, which
# is why these are a fingerprint and not something "restore" pretends to fix.
CODE_FILES = ("/usr/local/bin/bridge-feeder-audio.sh",
              "/usr/local/bin/bridge-return-audio.sh",
              "/usr/local/bin/bridge-feeder-net.sh",
              "/home/pi/uvc-raw-setup.sh")


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def _env(path, keys):
    """Parse KEY=VALUE lines. Absent file and absent key are both None — a config that has
    never been written must not look like one explicitly set to empty."""
    txt = _read(path) or ""
    out = {}
    for k in keys:
        m = re.search(r'^\s*%s\s*=\s*"?(.*?)"?\s*$' % re.escape(k), txt, re.M)
        out[k] = m.group(1) if m else None
    return out


def _sha(path):
    txt = _read(path)
    return hashlib.sha256(txt.encode()).hexdigest()[:12] if txt is not None else None


def _gadget_rates():
    """What the USB host is offered. The single most important line in the system: Windows
    only lists formats the device advertises, so a single-rate descriptor removes the
    feature entirely, and it has silently regressed once already."""
    out = {}
    for key in ("c_srate", "p_srate"):
        val = None
        for p in sorted(glob.glob("/sys/kernel/config/usb_gadget/*/functions/uac2.*/%s" % key)):
            v = _read(p)
            if v:
                val = v.strip()
                break
        out[key] = val
    return out


def collect():
    """The current state of everything the profile tracks."""
    return {
        "restorable": {
            "net": _env(NET_DEF, ("NET_VIDEO_LATENCY", "NET_AUDIO_LATENCY")),
            "tune": _env(TUNE_DEF, ("RETURN_SRC_PROPS", "RETURN_PRE_RESAMPLE")),
            "aec": _env(PEER_DEF, ("RETURN_AEC",)),
        },
        "fingerprint": {
            "gadget": _gadget_rates(),
            "code": {os.path.basename(p): _sha(p) for p in CODE_FILES},
            "version": (_read("/etc/bridge/version") or "").strip() or None,
        },
    }


def _flatten(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = "%s.%s" % (prefix, k) if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def diff():
    saved = load()
    cur = collect()
    if not saved:
        return {"saved": False, "state": "no baseline",
                "hint": "run: bridge golden save   (do it when the audio is known good)",
                "drift": [], "current": cur}
    a, b = _flatten(saved.get("state", {})), _flatten(cur)
    drift = []
    for key in sorted(set(a) | set(b)):
        was, now = a.get(key), b.get(key)
        if was == now:
            continue
        drift.append({
            "field": key,
            "golden": was,
            "current": now,
            # An operator's first question is "can I fix this from here?" — answer it in the
            # row itself rather than making them know which half of the profile a field is in.
            "fixable": key.startswith("restorable."),
        })
    n_fix = sum(1 for d in drift if d["fixable"])
    return {
        "saved": True,
        "saved_at": saved.get("saved_at"),
        "note": saved.get("note"),
        "state": "known-good" if not drift else "DRIFTED (%d)" % len(drift),
        "drift": drift,
        "restorable_count": n_fix,
        "needs_deploy_count": len(drift) - n_fix,
        "current": cur,
    }


def load():
    """The active baseline: the operator's if they have saved one, else the factory floor.

    Returns the profile with `source` set to "operator" or "factory" so every caller can say
    WHICH baseline it is comparing against. "Matches the factory default" and "matches the
    state you verified last Tuesday" are different claims and must not read the same.
    """
    for path, src in ((GOLDEN, "operator"), (FACTORY, "factory")):
        txt = _read(path)
        if not txt:
            continue
        try:
            prof = json.loads(txt)
        except Exception:
            continue                      # a corrupt operator file must fall through, not win
        if isinstance(prof, dict) and prof.get("state"):
            prof["source"] = src
            return prof
    return None


def save(note=None, factory=False, state=None):
    """Write a baseline. `factory` targets the read-only root and is only writable at image
    build time; `state` lets the builder supply values it cannot measure on a build host."""
    target = FACTORY if factory else GOLDEN
    prof = {"saved_at": int(time.time()), "note": note or "",
            "state": state if state is not None else collect()}
    tmp = target + ".tmp"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(prof, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)          # atomic: a half-written baseline is worse than none
    os.chmod(target, 0o644)          # bridge-web runs as 'pi' and must be able to read it
    return prof


def _write_env(path, pairs, header):
    body = header.rstrip() + "\n"
    for k, v in pairs:
        if v is not None:
            body += '%s="%s"\n' % (k, v)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def restore():
    saved = load()
    if not saved:
        return {"ok": False, "error": "no golden profile saved"}
    want = saved.get("state", {}).get("restorable", {})
    d = diff()
    changed, services = [], set()

    net = want.get("net") or {}
    cur_net = collect()["restorable"]["net"]
    if any(net.get(k) != cur_net.get(k) for k in ("NET_VIDEO_LATENCY", "NET_AUDIO_LATENCY")):
        _write_env(NET_DEF, [("NET_VIDEO_LATENCY", net.get("NET_VIDEO_LATENCY")),
                             ("NET_AUDIO_LATENCY", net.get("NET_AUDIO_LATENCY"))],
                   "# NetBridge media tuning (restored from golden profile)")
        changed.append("net")
        # The video feeder reads this file, so restoring it costs a video interruption. Say
        # so in the result rather than letting the operator discover it on a live call.
        services |= {"bridge-feeder-net", "bridge-feeder-audio"}

    tune = want.get("tune") or {}
    cur_tune = collect()["restorable"]["tune"]
    if any(tune.get(k) != cur_tune.get(k) for k in ("RETURN_SRC_PROPS", "RETURN_PRE_RESAMPLE")):
        _write_env(TUNE_DEF, [("RETURN_SRC_PROPS", tune.get("RETURN_SRC_PROPS")),
                              ("RETURN_PRE_RESAMPLE", tune.get("RETURN_PRE_RESAMPLE"))],
                   "# NetBridge return-audio tuning (restored from golden profile)")
        changed.append("tune")
        services.add("bridge-return-audio")

    for svc in sorted(services):
        subprocess.run(["systemctl", "restart", svc], capture_output=True)

    return {
        "ok": True,
        "restored": changed,
        "restarted": sorted(services),
        "video_interrupted": "bridge-feeder-net" in services,
        "still_drifted": [x for x in d["drift"] if not x["fixable"]],
        "note": ("nothing to restore — the restorable config already matches golden"
                 if not changed else "restored %s" % ", ".join(changed)),
    }


def _print_diff(d):
    if not d["saved"]:
        print("  no golden profile saved yet")
        print("  %s" % d["hint"])
        return
    when = time.strftime("%d %b %H:%M", time.localtime(d["saved_at"])) if d["saved_at"] else "?"
    print("  baseline saved %s%s" % (when, ("  — %s" % d["note"]) if d.get("note") else ""))
    if not d["drift"]:
        print("  \033[32mknown-good\033[0m — every tracked field matches the baseline")
        return
    print("  \033[33m%s\033[0m" % d["state"])
    for row in d["drift"]:
        tag = "restorable" if row["fixable"] else "NEEDS DEPLOY"
        print("    %-34s %-12s golden=%s  now=%s"
              % (row["field"], tag, row["golden"], row["current"]))
    if d["needs_deploy_count"]:
        print("  %d field(s) cannot be fixed from here: the code or the image differs."
              % d["needs_deploy_count"])
        print("  Use 'deploy signed script' or reflash — restoring configfs would be undone")
        print("  at the next boot and would look like a fix that did not happen.")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("action", choices=("save", "show", "diff", "restore"))
    ap.add_argument("--note", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.action == "save":
        p = save(a.note)
        print(json.dumps(p, indent=2) if a.json else
              "  saved golden profile (%d tracked fields)" % len(_flatten(p["state"])))
    elif a.action == "show":
        p = load()
        if not p:
            print("  no golden profile saved"); return 1
        print(json.dumps(p, indent=2) if a.json else json.dumps(p["state"], indent=2))
    elif a.action == "diff":
        d = diff()
        print(json.dumps(d, indent=2)) if a.json else _print_diff(d)
        return 0
    elif a.action == "restore":
        r = restore()
        if a.json:
            print(json.dumps(r, indent=2))
        else:
            print("  %s" % r.get("note") or r.get("error"))
            if r.get("restarted"):
                print("  restarted: %s" % ", ".join(r["restarted"]))
            if r.get("video_interrupted"):
                print("  NOTE: the video feeder was restarted — video froze for a few seconds")
            for x in r.get("still_drifted", []):
                print("  still drifted (needs deploy): %s golden=%s now=%s"
                      % (x["field"], x["golden"], x["current"]))
        return 0 if r.get("ok") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
