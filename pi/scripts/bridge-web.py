#!/usr/bin/env python3
"""NetBridge status dashboard - stdlib only.
Serves:
  GET /             HTML dashboard (auto-refresh)
  GET /api/status   full status JSON (also consumed by the fleet agent / control plane)
  GET /api/checks   presenter green checks (samples ~2s; kept out of gather() so
                    status/telemetry stay instant)
  GET /api/health   tiny liveness JSON
on http://<pi>:8080"""
import http.server, socketserver, subprocess, os, time, socket, json, hashlib, re, glob
import importlib.util

PORT = 8080
VERSION_FILE = "/etc/bridge/version"
SERVICES = ["bridge-gadget", "bridge-feeder-net", "bridge-uvcd",
            "bridge-feeder-audio", "bridge-return-audio"]

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""

def soc_temp():
    """SoC temperature. Prefer sysfs: it is world-readable, so it works even when
    /dev/vcio is not (bridge-web runs as 'pi'). vcgencmd stays as a fallback."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return "%.1f'C" % (int(f.read().strip()) / 1000.0)
    except Exception:
        pass
    return sh("vcgencmd measure_temp").replace("temp=", "") or "?"


def _valid_mask(s):
    """Normalise a throttle mask, or None if `s` is not one.

    Every source here can fail by RETURNING TEXT rather than by raising, and unvalidated
    text propagates as data. `vcgencmd` prints "Can't open device file: /dev/vcio_gencmd"
    on stdout and exits 0, so the old code stored that sentence in the `raw` field, handed
    it to int(), got ValueError, and reported "unreadable" with the error message sitting
    where a number belongs. Anything that is not a plausible mask is not a reading.
    """
    s = (s or "").strip().replace("throttled=", "").strip()
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1,8}", s or ""):
        return None
    try:
        return "0x%x" % int(s, 16)
    except ValueError:
        return None


def _flight_tail(window=500):
    """Last `window` lines of the flight recorder, or None if it cannot be read."""
    for path in FLIGHT_PATHS:
        try:
            with open(path, "rb") as f:
                try:
                    f.seek(-window * 64, 2)   # ~64 bytes/line, cheap bounded read
                except OSError:
                    f.seek(0)
                return f.read().decode("utf-8", "replace").splitlines()[-window:]
        except Exception:
            continue
    return None


def _flight_masks(lines):
    """(most recent mask, OR of every mask seen) from flight recorder lines."""
    last, sticky = None, 0
    for ln in lines or ():
        m = re.search(r"thr=0x([0-9a-fA-F]+)", ln)
        if not m:
            continue
        try:
            v = int(m.group(1), 16)
        except ValueError:
            continue
        last, sticky = v, sticky | v
    return last, sticky


def throttle_sources():
    """Every readable source of the throttle mask, best first, as (name, mask_int).

    WHY THERE IS MORE THAN ONE
    --------------------------
    bridge-web runs as `User=pi`. On the 2026-08-13 image `vcgencmd` needs /dev/vcio_gencmd,
    which `pi` cannot open, so the live power verdict went blind on a board that genuinely
    was browning out — while the fleet's brownout PERCENTAGE stayed correct, because that
    number is computed from flight.txt. Two views of one board disagreeing, with the more
    prominent one wrong.

    The flight recorder runs as root and writes `thr=0x…` to disk every second, so the mask
    this process cannot ask the firmware for is already sitting in a file it can read. That
    makes the fix a privilege-free one: no udev rule, no group change, no setuid helper, and
    nothing that has to be granted again on the next image.

    Ordering is sticky-first for the reason recorded in power_state(): the hwmon
    in0_lcrit_alarm node is INSTANTANEOUS, so it answers 0x0 on almost every poll. It used
    to be read first, it always answered, and the authoritative read was therefore never
    reached — the panel showed a healthy 0x0 on a board whose real value was 0x50000 while
    the recorder was catching live brownouts in 1.3% of sampled seconds. It is still read,
    but only as EXTRA information, never as a substitute for the history.
    """
    out = []
    # 1. sysfs — world-readable, no privileges, no subprocess. Globbed because the firmware
    #    node has moved between kernel versions and a hardcoded path silently yields nothing.
    seen_paths = set()
    for pat in ("/sys/devices/platform/soc/soc:firmware/get_throttled",
                "/sys/devices/platform/soc/*firmware*/get_throttled",
                "/sys/devices/platform/*firmware*/get_throttled"):
        for p_ in sorted(glob.glob(pat)):
            if p_ in seen_paths:
                continue
            seen_paths.add(p_)
            try:
                with open(p_) as f:
                    m = _valid_mask(f.read())
                if m is not None:
                    out.append(("sysfs", int(m, 16)))
                    break
            except Exception:
                pass
        if out:
            break
    # 2. the flight recorder — written by root every second, readable by anyone.
    last, sticky = _flight_masks(_flight_tail())
    if last is not None:
        out.append(("flight", last))
        if sticky != last:
            out.append(("flight-sticky", sticky))
    # 3. vcgencmd — authoritative when it works, but needs a device node `pi` may not have.
    m = _valid_mask(sh("vcgencmd get_throttled"))
    if m is not None:
        out.append(("vcgencmd", int(m, 16)))
    return out


def soc_throttled():
    """The throttle bitmask as a '0x…' string, or '?' if no source could be read."""
    src = throttle_sources()
    return ("0x%x" % src[0][1]) if src else "?"


def undervolt_now():
    """The coarse 'browning out at this instant' alarm, or None if unreadable."""
    for p_ in glob.glob("/sys/class/hwmon/hwmon*/in0_lcrit_alarm"):
        try:
            with open(p_) as f:
                return f.read().strip() == "1"
        except Exception:
            pass
    return None


# Raspberry Pi firmware throttle bits. The low nibble is "right now"; the 0x1xxxx nibble is
# "has happened since boot" and is the only part that survives the event you care about.
_THROTTLE_BITS = ((0x1, "under-voltage NOW"), (0x2, "ARM frequency capped NOW"),
                  (0x4, "throttled NOW"), (0x8, "soft temperature limit NOW"),
                  (0x10000, "under-voltage has occurred"),
                  (0x20000, "ARM frequency capping has occurred"),
                  (0x40000, "throttling has occurred"),
                  (0x80000, "soft temperature limit has occurred"))


# /home/pi/flight.txt is a symlink onto /data (the root is read-only). Older cards may not
# have the symlink, so try the real location too rather than silently reporting no data —
# "no rate available" and "rate is zero" must never look the same.
FLIGHT_PATHS = ("/home/pi/flight.txt", "/data/flight.txt")


def brownout_rate(window=500):
    """Percentage of recent seconds in which the board was ACTIVELY browning out.

    The sticky bits answer "has this ever happened", which is necessary but not sufficient:
    they stay set forever, so a board that browned out once at boot looks identical to one
    doing it constantly. What actually predicts audio quality is the RATE.

    Measured 2026-08-10 on this hardware: during the window Samith confirmed the audio was
    clean by ear, the rate was 0.50%. Over the whole recording it was 1.30%, and one 5000-
    sample stretch hit 3.62%. Same board, same sticky bits, audibly different results — so
    "has browned out" alone cannot tell you whether tonight will be good.

    Reads only the tail of the flight recorder, which is a ring of the last ~500 seconds.
    Returns None when unreadable rather than guessing; a missing recorder is not 0%.
    """
    lines = _flight_tail(window)
    if lines is None:
        return None
    seen = live = 0
    for ln in lines:
        m = re.search(r"thr=0x([0-9a-fA-F]+)", ln)
        if not m:
            continue
        seen += 1
        try:
            if int(m.group(1), 16) & 0x1:    # bit0 = under-voltage RIGHT NOW
                live += 1
        except ValueError:
            pass
    if not seen:
        return None
    return {"samples": seen, "live": live, "pct": round(100.0 * live / seen, 2)}


def power_state(raw=None):
    """Decode the throttle mask into something an operator can act on without a bundle.

    Returns ok=False when the board has EVER browned out, not merely when it is browning out
    as you look at it. A fault that shows up 1.3% of the time is still the fault.
    """
    srcs = throttle_sources()
    if raw is None:
        raw = ("0x%x" % srcs[0][1]) if srcs else None
    v = int(raw, 16) if _valid_mask(raw) else None
    if v is None and not srcs:
        # No source answered. Say so as an absence, and never echo a command's error text
        # back as if it were a reading — that is what made this field unreadable-looking
        # rather than obviously broken.
        return {"raw": None, "ok": None, "live": None, "ever": None,
                "rate": brownout_rate(), "source": None,
                "summary": "unreadable — no throttle source available", "flags": []}
    if v is None:
        v = srcs[0][1]
        raw = "0x%x" % v
    # The sticky "has occurred" bits cannot be wrong in the false-positive direction: no
    # source invents a brownout. So OR them across every source that answered. This is what
    # stops one blind reader (vcgencmd without its device node) from reporting a clean board
    # while another source on the same machine is holding the evidence.
    sticky_all = 0
    for _name, mv in srcs:
        sticky_all |= mv & 0xF0000
    v |= sticky_all
    flags = [name for bit, name in _THROTTLE_BITS if v & bit]
    live = bool(v & 0x1) or bool(v & 0x4) or undervolt_now() is True
    ever = bool(v & 0x10000) or bool(v & 0x40000)
    # Thermal is a separate fault with a separate fix; do not fold it into the power verdict,
    # but never report "clean" while a thermal bit is set — that reads as a contradiction.
    thermal = bool(v & 0x8) or bool(v & 0x80000)
    rate = brownout_rate()
    if rate and rate["pct"] >= 2.0:
        # Above ~2% the stutter is audible. Below ~1% this board has been confirmed clean
        # by ear with the sticky bits already set, so the rate is what to act on.
        summary = ("browning out %.1f%% of the time — enough to be audible; raise the "
                   "return buffer and expect possible reboots" % rate["pct"])
        return {"raw": raw, "ok": False, "live": live, "ever": True, "rate": rate,
                "source": srcs[0][0] if srcs else None,
                "summary": summary, "flags": flags}
    if live:
        summary = "browning out RIGHT NOW — expect stutter and possible reboots"
    elif ever:
        summary = ("this board HAS browned out since boot — audio stutter and spontaneous "
                   "reboots come from here, not from the network")
    elif thermal:
        summary = "power clean, but the SoC has hit its temperature limit — check airflow"
    else:
        summary = "power clean since boot"
    return {"raw": raw, "ok": not (live or ever), "live": live, "ever": ever,
            "rate": rate, "source": srcs[0][0] if srcs else None,
            "summary": summary, "flags": flags}


_GOLDEN_PY = "/usr/local/bin/bridge-golden.py"
_golden_mod = None


def golden_state():
    """A compact 'has this bridge drifted from its known-good config?' for the fleet row.

    Loaded once and called in-process: /api/status is polled every few seconds by both the
    panel and the fleet agent, and spawning a Python interpreter per poll to answer a
    question about four small files would be a silly cost to pay forever.

    Any failure is reported as "unknown" with the reason. It must never raise — a missing or
    broken baseline is a nice-to-have going absent, and taking the whole status endpoint down
    with it would turn a cosmetic gap into an outage.
    """
    global _golden_mod
    try:
        if _golden_mod is None:
            if not os.path.exists(_GOLDEN_PY):
                return {"state": "unavailable"}
            spec = importlib.util.spec_from_file_location("bridge_golden", _GOLDEN_PY)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _golden_mod = m
        d = _golden_mod.diff()
        return {"state": d.get("state"), "drift": len(d.get("drift") or []),
                "restorable": d.get("restorable_count", 0),
                "needs_deploy": d.get("needs_deploy_count", 0),
                "saved_at": d.get("saved_at"), "note": d.get("note")}
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:120]}


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""

def wifi_dbm():
    try:
        lines = read("/proc/net/wireless").splitlines()
        for ln in lines[2:]:
            parts = ln.split()
            if parts:
                return parts[3].rstrip(".")
    except Exception:
        pass
    return ""

def cpu_serial():
    # Stable per-Pi hardware id. Falls back to the machine-id, then hostname.
    for ln in read("/proc/cpuinfo").splitlines():
        if ln.lower().startswith("serial"):
            v = ln.split(":", 1)[1].strip()
            if v and set(v) != {"0"}:
                return v
    mid = read("/etc/machine-id")
    return mid or sh("hostname")

def pairing_code(serial):
    # Human-friendly claim code printed on a sticker / shown below. Deterministic
    # from the hardware id so it survives reflash: BRIDGE-XXXX (hex).
    h = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:4].upper()
    return "BRIDGE-" + h

def tailscale_ip4():
    return sh("tailscale ip -4 2>/dev/null").splitlines()[0] if sh("tailscale ip -4 2>/dev/null") else ""

def svc_restarts(svc):
    v = sh("systemctl show -p NRestarts --value %s" % svc)
    try:
        return int(v)
    except Exception:
        return 0

def clock_verdict():
    """Return (bool suspect, dict detail) for the UAC2 gadget clock. Prefers the real
    FFT verdict the crackle-sentry writes to /run/bridge/crackle.json (M4); the file
    exists only while a crackle is currently latched (edge-triggered) and is removed
    on resolve, so its mere presence — if fresh — means "crackling now". Falls back to
    the old I/O-error journal heuristic when the sentry isn't running."""
    try:
        st = os.stat("/run/bridge/crackle.json")
        if time.time() - st.st_mtime < 300:  # fresh within 5 min
            with open("/run/bridge/crackle.json") as f:
                d = json.load(f)
            return d.get("verdict") in ("crackle", "degrading"), d
    except FileNotFoundError:
        pass
    except Exception:
        pass
    errs = sh("journalctl -u bridge-return-audio --since '-10 min' 2>/dev/null "
              "| grep -ci 'input/output error'")
    try:
        return int(errs) > 0, {"verdict": "crackle" if int(errs) > 0 else "clean",
                               "source": "journal-heuristic"}
    except Exception:
        return False, {}


def clock_suspect():
    return clock_verdict()[0]

def gather():
    d = {}
    d["host"] = sh("hostname")
    hi = sh("hostname -I")
    d["ip"] = hi.split()[0] if hi else "?"
    d["services"] = [(s, sh("systemctl is-active %s" % s)) for s in SERVICES]
    state = ""
    udcdir = "/sys/class/udc"
    if os.path.isdir(udcdir):
        for f in os.listdir(udcdir):
            state = read("%s/%s/state" % (udcdir, f))
            d["speed"] = read("%s/%s/current_speed" % (udcdir, f))
            break
    d["udc"] = state or "?"
    d["functions"] = sh("ls /sys/kernel/config/usb_gadget/g1/functions/ 2>/dev/null").replace("\n", " ")
    d["video40"] = os.path.exists("/dev/video40")
    d["uac2"] = os.path.isdir("/proc/asound/UAC2Gadget")
    d["temp"] = soc_temp()
    d["throttled"] = soc_throttled()
    d["config"] = golden_state()
    # Decoded power verdict, including the STICKY history. Rides telemetry so the fleet can
    # show a brownout on the device's row instead of a green light that needs a diagnostics
    # bundle to contradict.
    d["power"] = power_state(d["throttled"])
    d["wifi"] = wifi_dbm()
    d["uptime"] = sh("uptime -p").replace("up ", "")
    peer = ""
    for ln in read("/etc/default/bridge-return-audio").splitlines():
        if ln.startswith("RETURN_DEST_IP"):
            peer = ln.split("=", 1)[1]
        if ln.startswith("RETURN_DEST_PORT"):
            peer += ":" + ln.split("=", 1)[1]
    d["peer"] = peer or "?"
    d["wd_timer"] = sh("systemctl is-active bridge-watchdog.timer")
    d["wd_hw"] = sh("systemctl show -p RuntimeWatchdogUSec --value")
    # PIN-gate state (rides telemetry so a brute-force lockout raises a fleet alert)
    try:
        d["pin"] = json.loads(sh("sudo -n /usr/local/bin/bridge-pin state"))
    except Exception:
        d["pin"] = {}
    # --- fleet identity + telemetry (consumed by the control plane) ---
    serial = cpu_serial()
    d["device_id"] = serial
    d["pairing_code"] = pairing_code(serial)
    d["version"] = read(VERSION_FILE) or "dev"
    d["tailscale_ip"] = tailscale_ip4()
    svc = dict(d["services"])
    # Per-stream up/down. Service "active" is the cheap proxy; a configured UDC means a
    # client is actually attached so the forward streams can land somewhere.
    attached = d["udc"] == "configured"
    d["streams"] = {
        "video":  svc.get("bridge-feeder-net") == "active" and attached,
        "voice":  svc.get("bridge-feeder-audio") == "active" and attached,
        "return": svc.get("bridge-return-audio") == "active" and attached,
    }
    d["restarts"] = {
        "feeder_net": svc_restarts("bridge-feeder-net"),
        "uvcd": svc_restarts("bridge-uvcd"),
        "return_audio": svc_restarts("bridge-return-audio"),
    }
    d["return_mismatch"] = _return_mismatch()   # None = healthy; dict = wrong-rate now
    d["return_rate"] = _return_opened_rate()    # what the pipeline is opened at (0=idle)
    d["mesh_path"] = mesh_path()                # direct vs DERP relay — the big one on a long link
    d["quarantined"] = _quarantined()           # [] = none; names = deployed code NOT running
    suspect, detail = clock_verdict()
    d["clock_suspect"] = suspect          # bool (backward compat for the control plane)
    d["clock"] = detail                   # M4: full FFT verdict {verdict,score,reasons,...}
    d["ts"] = int(time.time())
    return d

# --------------------------- presenter green checks ---------------------------
# Journey 3: four am-I-live booleans a presenter can trust. These sample real
# activity (CPU-tick and hw_ptr deltas), not lifetime averages (`ps pcpu` lies),
# so they block ~2s. That is why they live on /api/checks, NOT in gather().

PCM_RETURN_STATUS = "/proc/asound/UAC2Gadget/pcm0c/sub0/status"
RETURN_RUNDIR = "/run/bridge-return-audio"

def _return_opened_rate():
    """The rate the return pipeline opened the capture at (written by
    bridge-return-audio.sh). 0 = unknown/idle."""
    try:
        return int(open(RETURN_RUNDIR + "/rate").read().strip())
    except Exception:
        return 0

def _return_mismatch():
    """Watchdog-published rate-mismatch event, or None. Present only between the
    watchdog detecting device!=pipeline rate and the next successful re-open, so its
    mere existence means 'return audio is currently wrong-rate (self-heal underway)'."""
    try:
        return json.loads(open(RETURN_RUNDIR + "/mismatch").read())
    except Exception:
        return None

def mesh_path():
    """How the presenter's node is actually reached: a DIRECT hole-punched path, or relayed
    through a DERP server.

    This is the single biggest determinant of quality on a long link and until now it was
    invisible without SSHing in. India<->US direct is ~200-250ms; the same pair relayed is
    often 400ms+ because the traffic detours via a shared relay. A presenter needs to know
    which one they are on BEFORE a meeting, not after it goes badly — and an admin needs to
    see it for a bridge on another continent without touching it.

    Read from the BRIDGE's own tailscale, because it is the side that knows how it reaches
    the peer. Returns {} when there is no session, which the UI shows as nothing at all
    rather than a scary blank."""
    peer = ""
    for ln in read("/etc/default/bridge-return-audio").splitlines():
        if ln.startswith("RETURN_DEST_IP"):
            peer = ln.split("=", 1)[1].strip()
    if not peer:
        return {}
    for ln in sh("tailscale status 2>/dev/null").splitlines():
        if not ln.startswith(peer + " ") and not ln.startswith(peer + "\t"):
            continue
        low = ln.lower()
        if "relay" in low:
            m = re.search(r'relay "([^"]+)"', ln)
            return {"via": "relay", "detail": m.group(1) if m else "derp",
                    "note": "relayed — expect higher latency than a direct path"}
        if "direct" in low:
            m = re.search(r"direct ([0-9a-fA-F:.\[\]]+:\d+)", ln)
            return {"via": "direct", "detail": m.group(1) if m else "",
                    "note": "direct path — best possible latency for this link"}
        if "offline" in low:
            return {"via": "offline", "detail": "", "note": "peer not reachable"}
        return {"via": "idle", "detail": "", "note": "no active connection to the presenter"}
    return {}


def _quarantined():
    """Overrides auto-rollback has parked, i.e. code the operator deployed that is NOT
    running. Reported in telemetry because this is the quietest failure the device has:
    the bridge silently falls back to its baked-in script and keeps working, so nothing
    looks wrong on the panel while the fix you shipped is simply absent. It stayed
    invisible for a whole session before anyone thought to read a diagnostics bundle."""
    try:
        return sorted(n.split(".")[0] + ".sh"
                      for n in os.listdir("/data/overrides/quarantine")
                      if ".sig." not in n)
    except Exception:
        return []

def _udc_state():
    udcdir = "/sys/class/udc"
    if os.path.isdir(udcdir):
        for f in os.listdir(udcdir):
            return read("%s/%s/state" % (udcdir, f))
    return ""

def _feeder_cpu_ticks():
    """(pid, utime+stime clock ticks) of the net video feeder, or (None, None)."""
    pids = sh("pgrep -f 'udpsrc port=5000'").split()
    if not pids:
        return None, None
    stat = read("/proc/%s/stat" % pids[0])
    try:
        f = stat.rsplit(")", 1)[1].split()   # fields after comm; utime/stime = 14/15 1-indexed
        return pids[0], int(f[11]) + int(f[12])
    except Exception:
        return pids[0], None

def _voice_feeder_cpu_ticks():
    """(pid, utime+stime clock ticks) of the FORWARD-voice feeder, or (None, None).

    Exact mirror of the video feeder, on the voice RTP port. Burning CPU here means the
    feeder is actively decoding the presenter's mic RTP and pushing it to the gadget - i.e.
    the presenter's voice IS arriving at the bridge. Measured at the RECEIVER, like video,
    so it means "landed here", not merely "was sent"."""
    pids = sh("pgrep -f 'udpsrc port=5002'").split()
    if not pids:
        return None, None
    stat = read("/proc/%s/stat" % pids[0])
    try:
        f = stat.rsplit(")", 1)[1].split()
        return pids[0], int(f[11]) + int(f[12])
    except Exception:
        return pids[0], None


def _return_hw_ptr():
    """Capture-side (from client) hw_ptr in frames, or None if stream closed."""
    st = read(PCM_RETURN_STATUS)
    if not st or st.startswith("closed"):
        return None
    for ln in st.splitlines():
        if ln.startswith("hw_ptr"):
            try:
                return int(ln.split(":", 1)[1].split()[0])
            except Exception:
                return None
    return None

def checks():
    win = 2.0                       # one shared sampling window for all deltas
    pid, t0 = _feeder_cpu_ticks()
    vpid, vt0 = _voice_feeder_cpu_ticks()
    # Measure the REAL elapsed time between the two hw_ptr reads, not the sleep.
    #
    # This used to divide by `win` (a hardcoded 2.0s) while the numerator spanned the sleep
    # PLUS two _feeder_cpu_ticks() calls — each of which shells out to pgrep. Those ~60-120ms
    # sat inside the measurement and outside the divisor, so every reading came back ~6% high:
    # a healthy 48000Hz stream reported ~50800/s. That is not cosmetic. The rate-mismatch
    # alarm trips at 12%, so the artifact silently ate half the headroom, and whenever a pgrep
    # stalled a little longer the computed rate crossed the threshold and the panel showed
    # "RATE MISMATCH — audio is pitch-shifted" while the audio was perfectly fine. One sample
    # was observed at 58200/s, implying 2.43s of real elapsed against a 2.0s divisor.
    m0 = time.monotonic()
    p0 = _return_hw_ptr()
    time.sleep(win)
    _, t1 = _feeder_cpu_ticks()
    _, vt1 = _voice_feeder_cpu_ticks()
    p1 = _return_hw_ptr()
    elapsed = max(time.monotonic() - m0, 0.001)

    if pid is None:
        video_ok, video_detail = False, "video feeder process not running"
    elif t0 is None or t1 is None:
        video_ok, video_detail = False, "feeder pid %s: could not read /proc stat" % pid
    else:
        dt = t1 - t0
        video_ok = dt > 10          # >10 cpu ticks in 2s = actively decoding RTP
        video_detail = "feeder pid %s used %d cpu ticks in %.0fs" % (pid, dt, win)

    if p0 is None or p1 is None:
        audio_ok, audio_detail = False, ("the meeting laptop is not playing audio into NetBridge — "
                       "select NetBridge as its SPEAKER/output, then play something")
    else:
        dp = p1 - p0
        rate = dp / elapsed
        opened = _return_opened_rate()
        expect = opened or 48000
        # RATE-AWARE, not hardcoded: the old '>40000/s' threshold marked a perfectly
        # healthy 32 kHz session (~34k/s) as red. Flowing = within reach of the rate the
        # pipeline actually opened at.
        flowing = rate > 0.7 * expect
        # THE MISMATCH ALARM. Device pace vs pipeline caps disagreeing >12% is the
        # robotic/pitch bug (a mismatched-but-alive stream throws no error anywhere
        # else). hw_ptr advances at the DEVICE's true pace; `opened` is what the
        # pipeline believes. 50 RTP pkts/s is the same invariant seen from outside.
        mismatch = flowing and opened and abs(rate / opened - 1.0) > 0.12
        if mismatch:
            audio_ok = False
            audio_detail = ("RATE MISMATCH: device ~%d/s vs pipeline %d — audio is "
                            "pitch-shifted; self-heals within ~10s" % (rate, opened))
        else:
            audio_ok = flowing
            audio_detail = "hw_ptr advanced %d frames in %.2fs (~%d/s @ %s)" % (
                dp, elapsed, rate, ("%dHz" % opened) if opened else "?")

    if vpid is None:
        voice_ok, voice_detail = False, "voice feeder process not running"
    elif vt0 is None or vt1 is None:
        voice_ok, voice_detail = False, "voice feeder pid %s: could not read /proc stat" % vpid
    else:
        vdt = vt1 - vt0
        voice_ok = vdt > 4          # opus decode is lighter than h264; >4 ticks/2s = flowing
        voice_detail = "voice feeder pid %s used %d cpu ticks in %.0fs" % (vpid, vdt, win)

    udc = _udc_state()
    return {
        "online": {"ok": True, "detail": "bridge-web serving on :%d" % PORT},
        "video_arriving": {"ok": video_ok, "detail": video_detail},
        "voice_arriving": {"ok": voice_ok, "detail": voice_detail},
        "client_sees_camera": {"ok": udc == "configured",
                               "detail": "usb gadget state: %s" % (udc or "?")},
        "return_audio": {"ok": audio_ok, "detail": audio_detail},
        "ts": int(time.time()),
    }

def badge(ok, text):
    color = "#1a7f37" if ok else "#b42318"
    return '<span style="background:%s;color:#fff;padding:2px 9px;border-radius:10px;font-size:13px">%s</span>' % (color, text)

def page():
    d = gather()
    rows = ""
    for s, st in d["services"]:
        rows += "<tr><td>%s</td><td>%s</td></tr>" % (s, badge(st == "active", st))
    usb_ok = d["udc"] == "configured"
    html = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=3>
<title>NetBridge - %(host)s</title>
<style>body{font-family:system-ui,Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:18px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#8b949e;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%%;max-width:560px;margin-bottom:18px}
td{padding:7px 10px;border-bottom:1px solid #21262d;font-size:15px}
td:first-child{color:#8b949e}.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px 16px;max-width:560px;margin-bottom:14px}
.k{color:#8b949e}</style></head><body>
<h1>NetBridge &middot; %(host)s</h1>
<div class=sub>%(ip)s &nbsp;|&nbsp; updated %(now)s &nbsp;|&nbsp; auto-refresh 3s</div>
<div class=card><b>Services</b><table>%(rows)s</table></div>
<div class=card><b>USB client</b><br>
host: %(usb)s &nbsp; speed: %(speed)s<br>
functions: %(functions)s</div>
<div class=card><b>Devices</b><br>
loopback /dev/video40: %(v40)s &nbsp; UAC2 card: %(uac2)s &nbsp; return peer: %(peer)s</div>
<div class=card><b>Health</b><br>
temp: %(temp)s &nbsp; throttled: %(thr)s &nbsp; wifi: %(wifi)s dBm<br>
uptime: %(uptime)s &nbsp; watchdog: timer=%(wdt)s hw=%(wdh)s</div>
<div class=card><b>Fleet</b><br>
pairing code: <b>%(pair)s</b> &nbsp; version: %(ver)s<br>
tailscale: %(tsip)s &nbsp; clock: %(clock)s<br>
<span class=k>device id: %(devid)s</span></div>
</body></html>""" % {
        "host": d["host"], "ip": d["ip"], "now": time.strftime("%H:%M:%S"),
        "rows": rows, "usb": badge(usb_ok, d["udc"] + (" (no client)" if not usb_ok else "")),
        "speed": d.get("speed", "?"), "functions": d["functions"] or "-",
        "v40": badge(d["video40"], "ok" if d["video40"] else "missing"),
        "uac2": badge(d["uac2"], "ok" if d["uac2"] else "missing"),
        "peer": d["peer"], "temp": d["temp"], "thr": d["throttled"],
        "wifi": d["wifi"] or "-", "uptime": d["uptime"],
        "wdt": d["wd_timer"], "wdh": d["wd_hw"],
        "pair": d["pairing_code"], "ver": d["version"],
        "tsip": d["tailscale_ip"] or "-",
        "clock": badge(not d["clock_suspect"], "ok" if not d["clock_suspect"] else "suspect"),
        "devid": d["device_id"],
    }
    return html

class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/status":
            self._send(json.dumps(gather()).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/checks":
            self._send(json.dumps(checks()).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/health":
            self._send(json.dumps({"ok": True, "ts": int(time.time())}).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/lock-state":
            # Is this bridge currently PIN-gated? Read straight from the device gate.
            st = sh("sudo -n /usr/local/bin/bridge-pin state")
            try:
                obj = json.loads(st)
            except Exception:
                obj = {"pin_set": False, "locked": False, "lockout": False, "lockout_remaining": 0}
            self._send(json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(page().encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/set-peer":
            # Register the presenter as the return-audio destination — the SSH-free
            # replacement for `ssh pi@bridge bridge set-peer <ip>`. Strictly a
            # dotted-quad (we only ever point at a mesh IP), and idempotent: if the
            # peer is already this ip:port we skip the return-audio restart so
            # re-going-live never blips the meeting audio.
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(n) if n else b"{}") or {}
                ip = str(body.get("ip", "")).strip()
                port = str(int(body.get("port", 5004)))
            except Exception:
                ip, port = "", "5004"
            parts = ip.split(".")
            valid = (len(parts) == 4 and
                     all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))
            if not valid:
                self._send(json.dumps({"ok": False, "error": "bad ip"}).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            cur = read("/etc/default/bridge-return-audio")
            if ("RETURN_DEST_IP=%s" % ip) in cur and ("RETURN_DEST_PORT=%s" % port) in cur:
                self._send(json.dumps({"ok": True, "changed": False,
                                       "peer": "%s:%s" % (ip, port)}).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            try:
                r = subprocess.run(["sudo", "-n", "/usr/local/bin/bridge", "set-peer", ip, port],
                                   capture_output=True, text=True, timeout=20)
                ok = r.returncode == 0
            except Exception:
                ok = False
            self._send(json.dumps({"ok": ok, "changed": True,
                                   "peer": "%s:%s" % (ip, port)}).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/return-tune":
            # Pipeline experiment knobs for the followed-rate clicks — the SSH-free way to
            # A/B alsasrc/resampler candidates (walkthrough J3 step 6 quality work). Same
            # trust level as set-peer: reachable only on :8080 (mesh/LAN). The charset is
            # allow-listed BOTH here and in the CLI because the values are word-split into
            # a gst-launch command line on the device.
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(n) if n else b"{}") or {}
                props = str(body.get("props", "")).strip()
                pre = str(body.get("pre", "")).strip()
                clear = bool(body.get("clear"))
            except Exception:
                props, pre, clear = "", "", False
            ok_chars = re.compile(r"^[a-zA-Z0-9=_.! -]*$")
            if not clear and (not ok_chars.match(props) or not ok_chars.match(pre)):
                self._send(json.dumps({"ok": False, "error": "illegal characters"}).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            args = (["return-tune", "clear"] if clear
                    else ["return-tune", props, pre])
            try:
                r = subprocess.run(["sudo", "-n", "/usr/local/bin/bridge"] + args,
                                   capture_output=True, text=True, timeout=25)
                ok = r.returncode == 0
            except Exception:
                ok = False
            self._send(json.dumps({"ok": ok, "clear": clear, "props": props,
                                   "pre": pre}).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/unlock":
            # The PIN is verified ON THE DEVICE ITSELF — it is never forwarded to
            # the control plane and never logged here. Presenter app -> this bridge
            # over the private mesh only.
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                pin = str((json.loads(self.rfile.read(n) if n else b"{}") or {}).get("pin", ""))
            except Exception:
                pin = ""
            try:
                r = subprocess.run(["sudo", "-n", "/usr/local/bin/bridge-pin", "unlock", pin],
                                   capture_output=True, text=True, timeout=20)
                code = r.returncode
                msg = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
            except Exception:
                code, msg = 4, "unlock failed on device"
            reason = {0: "ok", 1: "wrong", 2: "locked_out_now", 3: "locked_out"}.get(code, "error")
            self._send(json.dumps({"ok": code == 0, "reason": reason, "message": msg}).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(json.dumps({"ok": False, "error": "not found"}).encode("utf-8"),
                       "application/json; charset=utf-8")

    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    address_family = socket.AF_INET6
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()

if __name__ == "__main__":
    Server(("::", PORT), H).serve_forever()
