#!/usr/bin/env python3
"""NetBridge status dashboard - stdlib only.
Serves:
  GET /             HTML dashboard (auto-refresh)
  GET /api/status   full status JSON (also consumed by the fleet agent / control plane)
  GET /api/checks   presenter green checks (samples ~2s; kept out of gather() so
                    status/telemetry stay instant)
  GET /api/health   tiny liveness JSON
on http://<pi>:8080"""
import http.server, socketserver, subprocess, os, time, socket, json, hashlib, re, glob, shlex
import importlib.util, threading, copy

PORT = 8080
# The version and build record of the slot that is RUNNING, first (2026-09-28). /etc/bridge is
# bind-mounted from /data, which both A/B slots share: its copy is the version the card was
# FLASHED with and never changed after an OS update, so a bridge on 2.2.1 kept reporting 2.2.0 -
# the panel and `nb ota` were wrong, and a rollout re-targeted bridges already on its version.
# /etc/netbridge-image-version is on the slot's own root (build-disk-image.sh at flash,
# bridge-update.sh for the slot it installs), so it is right after a commit and a rollback alike.
# The /etc/bridge copies stay as the fallback for images that lack the per-slot files.
VERSION_FILES = ("/etc/netbridge-image-version", "/etc/bridge/version")
RELEASE_FILES = ("/etc/netbridge-release.json", "/etc/bridge/release.json")
SERVICES = ["bridge-gadget", "bridge-feeder-net", "bridge-uvcd",
            "bridge-feeder-audio", "bridge-return-audio"]

def _log(msg):
    """Journal line. Defined before its first use - an undefined name inside sh()'s try block
    would have been swallowed by the bare except and the refusal would have been silent."""
    try:
        subprocess.run(["logger", "-t", "bridge-web", str(msg)[:200]], timeout=3)
    except Exception:
        pass


def sh(cmd):
    """Run a fixed command and return its stdout.

    Deliberately NOT shell=True. Nothing request-derived reaches this function today - every
    caller interpolates an internal service name from a constant list - but this is the one
    process on the bridge that accepts unauthenticated network input, and a shell here is one
    careless edit away from being a command-injection hole. Splitting to argv removes the
    possibility rather than relying on every future caller being careful.

    Callers pass a string for readability; shlex.split gives the same words the shell would
    have produced for these commands, without a shell being involved.
    """
    try:
        if isinstance(cmd, (list, tuple)):
            argv = list(cmd)
        else:
            # Refuse rather than mis-execute. Without this, a future caller writing
            # "foo | bar" would silently get "|" and "bar" as arguments to foo and an empty
            # result - a failure that looks exactly like the command returning nothing, which
            # is the hardest kind to notice. Redirections were removed from every call site
            # because capture_output already separates stderr.
            if any(c in cmd for c in ("|", ">", "<", "&&", ";", "$(", "`")):
                _log("sh(): refusing a command that needs a shell: %s" % cmd[:80])
                return ""
            argv = shlex.split(cmd)
        return subprocess.run(argv, capture_output=True,
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
    m = _vcgencmd_throttled_mask()
    if m is not None:
        out.append(("vcgencmd", int(m, 16)))
    return out


def _vcgencmd_throttled_mask():
    """vcgencmd's mask, launched at most once a second (throttle_sources runs twice per status
    build) - and, when it fails, at most every 5 minutes. bridge-web runs as `pi`, which on
    these images usually cannot open /dev/vcio, so vcgencmd prints an error: relaunching it on
    every request bought nothing. The flight recorder (root) remains the primary source."""
    now = time.monotonic()
    hit = _CACHE.get("vcgencmd_throttled")
    if hit is not None and now - hit[0] < (1 if hit[1] is not None else 300):
        return hit[1]
    m = _valid_mask(sh("vcgencmd get_throttled"))
    _CACHE["vcgencmd_throttled"] = (now, m)
    return m


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
        # Sampling records electrical state, not audible quality or root cause of every
        # media fault. A larger audio buffer cannot repair an unstable power path.
        summary = ("under-voltage in %.1f%% of recent samples — check the power path; "
                   "media disruption or reboots are possible" % rate["pct"])
        return {"raw": raw, "ok": False, "live": live, "ever": True, "rate": rate,
                "source": srcs[0][0] if srcs else None,
                "summary": summary, "flags": flags}
    if live:
        summary = "under-voltage or throttling RIGHT NOW — check the power path; media disruption or reboots are possible"
    elif ever:
        summary = ("under-voltage has occurred since boot — check the recent samples and power path; "
                   "this history alone does not identify the cause of a media fault")
    elif thermal:
        summary = "power clean, but the SoC has hit its temperature limit — check airflow"
    else:
        summary = "power clean since boot"
    return {"raw": raw, "ok": not (live or ever), "live": live, "ever": ever,
            "rate": rate, "source": srcs[0][0] if srcs else None,
            "summary": summary, "flags": flags}


PRESENTER_TUNE_FILE = "/data/presenter-tuning.json"


# One-shot units that run ONLY on the boot after a flash. Flashing rewrites the whole disk,
# /data included, so the .expanded guard is wiped too and the filesystem expansion runs again
# every time - it is not a once-per-card event.
FIRSTBOOT_UNITS = ("bridge-firstboot", "bridge-regen-hostkeys", "bridge-firstdiag")


# ---------------------------------------------------------------------------------------
# WHO IS ALLOWED TO CHANGE THINGS
#
# Until 2026-08-25 this server authenticated nothing. `POST /api/set-peer` takes an IP and
# repoints the bridge's return audio at it - and the return stream is the meeting room's
# microphone. Any host that could reach port 8080, which on a venue LAN is every device on the
# guest network, could redirect a room's audio to itself with no credential and nothing in a
# log an operator would look at. For a product that sits in other organisations' meeting rooms
# that was the finding that blocked release.
#
# The fix is a trust boundary rather than a new credential system, because the architecture
# already has one. The presenter app does NOT talk to this port over the LAN: it goes through
# the mesh helper's local proxy (127.0.0.1:<control_port>) and arrives over tsnet, so it
# reaches us from a tailnet address. Traffic arriving from the LAN is, by construction, not
# the app.
#
# So: READS stay open (the fleet, the operator and the diagnostics tools depend on them, and
# they disclose no secret - the setup passphrase is deliberately excluded from /api/status).
# WRITES require the caller to be on loopback or the tailnet.
#
# Source-address enforcement rather than binding to the tailnet interface, deliberately:
# binding would race tailscaled at boot, would need rebinding whenever the tailnet address
# changes (it changed twice during testing), and would break read-only LAN diagnostics that
# are genuinely useful. This is checked per request, so there is nothing to race.
import ipaddress
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")   # CGNAT range tailscale hands out


def _parse_ip(addr):
    """The caller's address as an IP, IPv4-mapped IPv6 (what a dual-stack bind reports) unwrapped.
    None for anything that is not an address."""
    a = str(addr or "").strip()
    if a.lower().startswith("::ffff:") and "." in a:
        a = a[7:]
    try:
        return ipaddress.ip_address(a)
    except ValueError:
        return None


def _is_local(addr):
    """The bridge itself. Decided by PARSING the address, never by its text: a prefix check let
    "::1:2:3:4" and "::127.0.0.1" pass as loopback (2026-09-25 audit)."""
    ip = _parse_ip(addr)
    return bool(ip and ip.is_loopback)


def _mesh_or_local(addr):
    """Is this caller on the mesh (or the device itself)? 100.64.0.0/10 only - 100.1.2.3 is
    ordinary public address space, not the tailnet."""
    ip = _parse_ip(addr)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    return ip.version == 4 and ip in TAILNET_V4


def _audit(action, addr, allowed, detail=""):
    """Every mutation attempt, allowed or refused, goes to the journal with its source.

    A redirection of the room's audio previously left no trace. Refusals are logged too: a
    burst of them from one address is the only signal that someone is probing the bridge.
    """
    try:
        subprocess.run(["logger", "-t", "bridge-web",
                        "%s %s from=%s %s" % ("ALLOW" if allowed else "REFUSE",
                                              action, addr, detail)],
                       timeout=3)
    except Exception:
        pass


# ---------------------------------------------------------------------------------------
# THE PIN (2026-09-25)
#
# Being on the mesh was never meant to be enough to go live, but it was: set-peer did not ask
# whether the caller had unlocked, and the video/voice ports took packets from anyone, so the
# owner went live without ever typing the PIN. Now a correct PIN opens ONE session (bridge-pin
# unlock) and returns a ticket; set-peer and return-tune require that ticket; end-session
# closes it; bridge-pin's media gate admits video and voice only from that session's presenter.
#
# The PIN and the ticket reach bridge-pin on STDIN, never on its command line: sudo writes
# every command line to the journal, which is how every PIN attempt used to end up in the log.
# PIN verdicts (wrong / locked out / no PIN) stay HTTP 200 with a reason, as they always were:
# the app retries transport errors, and retrying a wrong PIN would burn all three tries.
PIN_TOOL = "/usr/local/bin/bridge-pin"
PIN_STATE_FILE = "/run/bridge-pin/state.json"
PIN_PROTOCOL = 2                  # the app refuses to unlock older bridges (their unlock restarted media)


def _caller_ip(addr):
    ip = _parse_ip(addr)
    return str(ip) if ip else str(addr or "")


def _pin_run(args, secret="", timeout=20):
    """bridge-pin as root, secret on stdin. -> (exit code, its JSON answer or {})"""
    try:
        r = subprocess.run(["sudo", "-n", PIN_TOOL] + list(args), input=(secret or "") + "\n",
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return 4, {"ok": False, "reason": "error", "message": "the PIN check could not run on the bridge"}
    try:
        j = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        j = {}
    return r.returncode, (j if isinstance(j, dict) else {})


def pin_state():
    """The PIN gate as bridge-pin last published it - read from a file, so a status poll costs no
    sudo and no process launch. Relative times are aged by the file's own age (it is rewritten
    at least every 15 s). An older image without the file falls back to asking the tool."""
    try:
        with open(PIN_STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict):
        try:
            return json.loads(_cached("pin", 3, lambda: sh("sudo -n %s state" % PIN_TOOL)))
        except Exception:
            return {}
    age = max(0, int(time.time() - float(st.pop("written_at", 0) or 0)))
    if age and st.get("lockout"):
        st["lockout_remaining"] = max(0, int(st.get("lockout_remaining") or 0) - age)
        st["lockout"] = st["lockout_remaining"] > 0
    ses = st.get("session") or {}
    if age and ses.get("active"):
        ses["age_s"] = int(ses.get("age_s") or 0) + age
        ses["expires_in"] = max(0, int(ses.get("expires_in") or 0) - age)
    st["stale_s"] = age
    return st


# ---------------------------------------------------------------------------------------
# WHAT THE USB SIDE IS ACTUALLY DOING
#
# `udc: not attached` was reported for five different situations - nothing plugged in, a
# charge-only cable, the meeting laptop asleep, the host refusing to enumerate, and a broken
# gadget - and in the fleet it reads like the bridge is offline. During the 2026-08-24 audit
# that cost twenty minutes: the bridge was healthy and the cable was simply out.
#
# The kernel already distinguishes more than we were using. /sys/class/udc/<udc>/state moves
# through: not attached -> attached -> powered -> default -> addressed -> configured, plus
# suspended. Each step tells you how far enumeration got, and therefore which end to look at.
#
# Where the hardware genuinely cannot tell two cases apart, this says so instead of guessing.
# A charge-only cable and an unplugged cable both leave the gadget with no data lines, and on
# a Pi 4 there is no VBUS sense line exposed to tell them apart - so both are reported as
# USB_DISCONNECTED with the ambiguity stated in the detail, rather than inventing certainty.
USB_STATES = {
    "configured": ("USB_CONNECTED_HEALTHY", True,
                   "the meeting laptop has enumerated the bridge and is using it"),
    "suspended":  ("USB_HOST_SUSPENDED", True,
                   "the meeting laptop has suspended the USB bus - it is probably asleep"),
    "addressed":  ("USB_HOST_NOT_ENUMERATING", True,
                   "the host assigned an address but never configured the device - "
                   "enumeration stalled on the laptop, not on the bridge"),
    "default":    ("USB_HOST_NOT_ENUMERATING", True,
                   "the host began enumeration and did not finish it"),
    "powered":    ("USB_HOST_NOT_ENUMERATING", True,
                   "bus power is present but the host has not started enumeration - "
                   "often a charge-only cable, or a port that does not carry data"),
    "attached":   ("USB_HOST_NOT_ENUMERATING", True,
                   "a host is attached but enumeration has not begun"),
    "not attached": ("USB_DISCONNECTED", False,
                   "no USB host. This looks the same whether the cable is out, the cable is "
                   "charge-only, or the laptop is powered off - the Pi cannot tell them "
                   "apart. Check the cable first; it is the most common cause."),
}


# ---------------------------------------------------------------------------------------
# IS THE VIDEO ANY GOOD, OR MERELY PRESENT?
#
# `video_arriving` has always been "is the feeder burning CPU". That answers whether something
# is happening and nothing about whether the meeting room is seeing a usable picture: a feeder
# decoding a stream that has collapsed to two frames a second burns CPU exactly like a healthy
# one. The audit could say video ARRIVES and could not say it was ACCEPTABLE.
#
# Bytes written to the loopback device is the honest measure, because a frame that reaches
# /dev/video40 is a frame the UVC gadget can hand to the laptop. It is raw YUY2, so the size
# is exactly known and frames-per-second follows by division - no estimation, no guessing.
#
# /proc/<pid>/io wchar counts bytes the process passed to write(), which for this pipeline is
# the v4l2sink. It costs one small read.
def _video_bytes(pid):
    if not pid:
        return None
    try:
        for ln in (read("/proc/%s/io" % pid) or "").splitlines():
            if ln.startswith("wchar:"):
                return int(ln.split(":", 1)[1].strip())
    except Exception:
        pass
    return None


def _video_frame_bytes():
    """Bytes per frame for the format actually configured, not an assumed one."""
    caps = read("/sys/devices/virtual/video4linux/video40/format") or ""
    m = re.search(r"(\d+)x(\d+)", caps)
    if m:
        return int(m.group(1)) * int(m.group(2)) * 2      # YUY2 = 2 bytes/pixel
    return 424 * 240 * 2                                   # the configured default


_VIDEO_SEEN = {}


def _expected_fps():
    """The frame rate the gadget was actually set up for.

    Read from the setup script rather than hard-coded, so changing the pipeline does not
    silently leave a health check comparing against a number nobody updated.
    """
    # The descriptor's dwFrameInterval (100 ns units) is the authority; the loopback caps
    # ("@N/1") agree with it. Until 2026-09-24 this read the descriptor script, found neither
    # pattern in it, and silently returned the fallback whatever the gadget was set up for.
    try:
        uvc = read("/home/pi/uvc-raw-setup.sh") or ""
        m = re.search(r"dwFrameInterval\s*\n(\d+)\s*\nEOF", uvc)
        if m and int(m.group(1)) > 0:
            return int(round(1e7 / int(m.group(1))))
        m = re.search(r"@(\d+)/1", read("/usr/local/bin/bridge-gadget-setup.sh") or "")
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 30


def video_throughput(pid, now):
    """Frames per second actually delivered to the gadget, or None.

    A DELTA between polls, like the stream-liveness check, so it costs nothing and cannot lie
    about a moment it did not observe. The first poll after a restart returns None rather than
    inventing a rate from one reading.
    """
    b = _video_bytes(pid)
    prev = _VIDEO_SEEN.get("v")
    _VIDEO_SEEN["v"] = (b, now)
    if b is None or not prev or prev[0] is None:
        return None
    ob, ot = prev
    gap = now - ot
    if gap <= 0 or gap > 120:
        return None
    per_frame = _video_frame_bytes() or 1
    return round((b - ob) / gap / per_frame, 1)


def usb_diagnosis(state, functions, video40, uac2):
    """Classify the USB side, and admit when the answer is ambiguous.

    Returns diagnosis / certain / detail. `certain` is False when the hardware genuinely
    cannot distinguish the possibilities - an operator is better served by "one of these
    three, check the cable first" than by a confident wrong answer.
    """
    # A gadget that never built is a bridge-side fault and must not be reported as a cable
    # problem: the states above all assume the gadget exists to be enumerated.
    if not (video40 and uac2 and functions and "uac2" in functions):
        return ("USB_GADGET_FAULT", True,
                "the USB gadget did not build on the bridge (uac2/uvc missing) - this is a "
                "bridge fault, not a cable or laptop one")
    d, certain, detail = USB_STATES.get((state or "").strip(),
                                        ("UNKNOWN", False,
                                         "the kernel reports an unrecognised UDC state (%r)"
                                         % state))
    return (d, certain, detail)


def settling():
    """Is the Pi still doing its post-flash work?

    WHY THIS EXISTS
    ---------------
    On 2026-08-24 an operator flashed a card, went live immediately, and heard loud periodic
    bursts that stopped by themselves a few minutes later. Nothing was wrong with the audio
    path: the Pi was expanding the /data filesystem (resize2fs, heavily IO-bound) and
    generating a full set of SSH host keys (ssh-keygen -A, CPU-bound) while the media
    pipeline was trying to keep a real-time deadline on the same four cores.

    Every reading looked healthy throughout, because none of them measured "this machine is
    busy with something that will finish on its own". So an operator has no way to tell a
    settling bridge from a broken one, and the natural response to bursts is to start
    changing settings that were never the problem.

    Cheap by construction: the answer can only be true shortly after a boot, so past that
    it returns False without spawning anything.
    """
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except Exception:
        return False
    if up > 900:            # 15 minutes; the work above finishes in one or two
        return False
    if sh("systemctl is-system-running") == "starting":
        return True
    for u in FIRSTBOOT_UNITS:
        if sh("systemctl is-active %s" % u) == "activating":
            return True
    return False


def presenter_tuning():
    """What the fleet wants the PRESENTER APP to apply, or None.

    WHY THIS EXISTS — the knob is on the wrong machine
    --------------------------------------------------
    Room audio is decoded on the presenter's laptop, through the APP's own jitter buffer
    (source_app.py: return_jitter_ms, default 250ms). The bridge's rtpjitterbuffer is the
    other direction entirely — the presenter's voice arriving here, which is what the room
    hears.

    So when an operator says "I can hear jitter", the buffer that would absorb it is on
    their Mac, and every action in the fleet menu tunes the direction they are not hearing.
    `profile:wan` cannot help, and clicking it costs a five-second video freeze to change
    nothing they will notice.

    There is no inbound path to a laptop behind NAT, so the fleet cannot push to it. But the
    app already POLLS /api/checks every 10s (BridgeWatch). Publishing the desired tuning in
    that response turns a fleet click into a change on the presenter's machine within one
    poll, with no restart of anything on the bridge and no interruption to video.

    The bridge is a courier here and nothing more. It does not interpret these values, and
    the app clamps them (60-1000ms) before use — a bridge should not be able to make a
    laptop do something unbounded just because it was asked to carry a number.
    """
    txt = None
    for p in (PRESENTER_TUNE_FILE, "/home/pi/presenter-tuning.json"):
        try:
            with open(p) as f:
                txt = f.read()
            break
        except Exception:
            continue
    if not txt:
        return None
    try:
        d = json.loads(txt)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


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


BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""

def first_read(paths):
    """The first of `paths` that exists and is not empty ("" if none)."""
    for p in paths:
        v = read(p)
        if v:
            return v
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

# WHY THE CACHES (2026-09-22). Measured on a live bridge: bridge-web.py averaged ~33% of a core,
# more than either audio pipeline, because every /api/status built its answer by launching ~20
# programs (hostname, one systemctl per service, tailscale twice, pgrep twice, vcgencmd, sudo,
# a refused journal pipeline plus a logger call to report the refusal). It is polled every few
# seconds by the presenter app, the fleet agent and diagnostic tools, on a 900 MHz-capped Pi
# that browns out under load. Values that change slowly are now remembered for a few seconds.
_CACHE = {}

def _cached(key, ttl, fn):
    """fn() at most once per ttl seconds; a failed call is not remembered as a result."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    v = fn()
    _CACHE[key] = (now, v)
    return v

def tailscale_ip4():
    # One call, not two: this used to run `tailscale ip -4` twice per request.
    out = _cached("tailscale_ip4", 30, lambda: sh("tailscale ip -4"))
    return out.splitlines()[0] if out else ""

def svc_restarts(svc):
    v = _cached("nrestarts:" + svc, 10,
                lambda: sh("systemctl show -p NRestarts --value %s" % svc))
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
    # This heuristic was a shell pipeline (journalctl | grep -ci). sh() refuses pipelines on
    # purpose, so for months it returned "" -> {} on every request, and _log()'d the refusal
    # by launching `logger` each time. Same question, no shell, counted in Python, and asked
    # at most once a minute: a 10-minute window does not need re-reading every few seconds.
    def _count():
        out = sh(["journalctl", "-u", "bridge-return-audio", "--since", "-10 min",
                  "--no-pager", "-o", "cat"])
        return sum(1 for ln in out.splitlines() if "input/output error" in ln.lower())
    errs = _cached("clock_journal_errs", 60, _count)
    return errs > 0, {"verdict": "crackle" if errs > 0 else "clean",
                      "source": "journal-heuristic"}


def clock_suspect():
    return clock_verdict()[0]

def _services_active():
    """All SERVICES in ONE systemctl call (it prints one state per unit, in order)."""
    out = sh(["systemctl", "is-active"] + list(SERVICES)).splitlines()
    if len(out) == len(SERVICES):
        return list(zip(SERVICES, out))
    return [(s, sh("systemctl is-active %s" % s)) for s in SERVICES]   # unexpected output

# The whole answer is rebuilt at most every _STATUS_TTL seconds; requests in between (the app,
# the fleet agent and a diagnostic tool polling at once) share it. Short enough that every
# consumer still sees streams change within a couple of seconds.
_STATUS_TTL = 2.0
_status_lock = threading.Lock()
_status_cache = [0.0, None]

def gather():
    with _status_lock:
        t, v = _status_cache
        if v is None or time.monotonic() - t >= _STATUS_TTL:
            v = _gather_uncached()
            _status_cache[0], _status_cache[1] = time.monotonic(), v
        return copy.deepcopy(v)     # callers may annotate their copy

def _gather_uncached():
    d = {}
    d["host"] = _cached("host", 60, lambda: sh("hostname"))
    hi = _cached("hostname_I", 15, lambda: sh("hostname -I"))
    d["ip"] = hi.split()[0] if hi else "?"
    d["services"] = _services_active()
    state = ""
    udcdir = "/sys/class/udc"
    if os.path.isdir(udcdir):
        for f in os.listdir(udcdir):
            state = read("%s/%s/state" % (udcdir, f))
            d["speed"] = read("%s/%s/current_speed" % (udcdir, f))
            break
    d["udc"] = state or "?"
    try:
        d["functions"] = " ".join(sorted(os.listdir("/sys/kernel/config/usb_gadget/g1/functions/")))
    except Exception:
        d["functions"] = ""
    d["video40"] = os.path.exists("/dev/video40")
    d["uac2"] = os.path.isdir("/proc/asound/UAC2Gadget")
    # AFTER functions/video40/uac2 are known - placing this above them made every reading say
    # USB_GADGET_FAULT, because the gadget check read a dict key that had not been filled in
    # yet. Kept alongside the raw `udc` value rather than replacing it: existing consumers
    # read that, and the raw kernel string is still the most useful thing when debugging.
    _dg, _certain, _detail = usb_diagnosis(state, d["functions"], d["video40"], d["uac2"])
    d["usb"] = {"state": state or "?", "diagnosis": _dg,
                "certain": _certain, "detail": _detail}
    d["temp"] = _cached("temp", 5, soc_temp)
    d["throttled"] = soc_throttled()
    d["config"] = golden_state()
    d["pcm"] = _return_pcm()
    # Decoded power verdict, including the STICKY history. Rides telemetry so the fleet can
    # show a brownout on the device's row instead of a green light that needs a diagnostics
    # bundle to contradict.
    d["power"] = power_state(d["throttled"])
    d["wifi"] = wifi_dbm()
    d["uptime"] = _cached("uptime", 30, lambda: sh("uptime -p")).replace("up ", "")
    # Which boot this is. The fleet counted reboots only from the restart counters falling, and a
    # healthy bridge's counters read 0 before and after every brownout reset - so a bridge
    # resetting every minute never raised its restart_storm alert (audit, 2026-09-28). The
    # kernel's boot_id changes on every boot. One small file read, no program launched.
    d["boot_id"] = _cached("boot_id", 3600, lambda: read(BOOT_ID_FILE))
    peer = ""
    for ln in read("/etc/default/bridge-return-audio").splitlines():
        if ln.startswith("RETURN_DEST_IP"):
            peer = ln.split("=", 1)[1]
        if ln.startswith("RETURN_DEST_PORT"):
            peer += ":" + ln.split("=", 1)[1]
    d["peer"] = peer or "?"
    d["wd_timer"] = _cached("wd_timer", 30, lambda: sh("systemctl is-active bridge-watchdog.timer"))
    d["wd_hw"] = _cached("wd_hw", 30, lambda: sh("systemctl show -p RuntimeWatchdogUSec --value"))
    # PIN gate (rides telemetry: lockouts, missing PIN, gate health and the live session all
    # reach the fleet). Read from bridge-pin's public state file - no sudo per request.
    d["pin"] = pin_state()
    # --- fleet identity + telemetry (consumed by the control plane) ---
    serial = cpu_serial()
    d["device_id"] = serial
    d["pairing_code"] = pairing_code(serial)
    d["version"] = first_read(VERSION_FILES) or "dev"
    # Full provenance, so the fleet can answer "what EXACTLY is this device running?" without
    # a human recognising a 7-character prefix. Images built before 2026-08-26 have no such
    # record, hence the tolerant default: a missing record reports as unknown, never invented.
    try:
        d["build"] = json.loads(first_read(RELEASE_FILES) or "{}") or {"version": d["version"]}
    except Exception:
        d["build"] = {"version": d["version"], "note": "release.json unreadable"}
    d["tailscale_ip"] = tailscale_ip4()
    svc = dict(d["services"])
    # Per-stream up/down, from counters that MOVE. A configured UDC is still required — a
    # forward stream with nowhere to land is not live — but it is no longer sufficient.
    # See _stream_live() for why "the service is active" was not a usable test.
    attached = d["udc"] == "configured"
    _now = time.monotonic()
    _, _vt = _feeder_cpu_ticks()
    _, _at = _voice_feeder_cpu_ticks()
    _rp = _return_hw_ptr()
    d["settling"] = settling()
    d["streams"] = {
        "video":  attached and svc.get("bridge-feeder-net") == "active"
                  and _stream_live("video", _vt, _now),
        "voice":  attached and svc.get("bridge-feeder-audio") == "active"
                  and _stream_live("voice", _at, _now),
        "return": attached and svc.get("bridge-return-audio") == "active"
                  and _stream_live("return", _rp, _now),
    }
    d["restarts"] = {
        "feeder_net": svc_restarts("bridge-feeder-net"),
        "uvcd": svc_restarts("bridge-uvcd"),
        "return_audio": svc_restarts("bridge-return-audio"),
    }
    d["return_mismatch"] = _return_mismatch()   # None = healthy; dict = wrong-rate now
    d["return_rate"] = _return_opened_rate()    # what the pipeline is opened at (0=idle)
    d["mesh_path"] = _cached("mesh_path", 10, mesh_path)   # direct vs DERP relay (runs `tailscale status`)
    d["quarantined"] = _quarantined()           # [] = none; names = deployed code NOT running
    # Signed updates: which are in effect, which wait for their moment, and safe mode
    # (bridge-overrides.sh). Whole-OS updates: the last OTA state (bridge-update.sh / bridge-ab).
    d["overrides"] = _json_file("/run/bridge-overrides/status.json")
    d["ota"] = _json_file("/data/ota-staging/status.json")
    # The two numbers the fleet's usb_misses and disk_low alerts read. Both alerts existed while
    # nothing reported either field, so neither could ever fire (found 2026-09-25).
    d["usb_misses_per_s"] = _usb_misses_per_s()
    d["data_free_mb"] = _data_free_mb()
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
    for ln in sh("tailscale status").splitlines():
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
        # "<name>.<timestamp>" — names are not only *.sh any more (bridge-web.py, keys, drop-ins)
        return sorted(set(n.rsplit(".", 1)[0]
                          for n in os.listdir("/data/overrides/quarantine")
                          if ".sig." not in n and n.rsplit(".", 1)[-1].isdigit()))
    except Exception:
        return []


USB_VIDEO_STATS = "/run/netbridge-usb-video.txt"


def _usb_misses_per_s():
    """Missed USB video transfers per second over the camera service's last 10 s window
    (bridge-uvcd's kernel-log counter writes one line every 10 s). None when the counter is
    not running or its newest line is stale - "unknown" must never read as "zero misses"."""
    try:
        if time.time() - os.stat(USB_VIDEO_STATS).st_mtime > 35:
            return None
        with open(USB_VIDEO_STATS) as f:
            last = f.read().splitlines()[-1]
        vals = dict(kv.split("=", 1) for kv in last.split()[1:] if "=" in kv)
        return round(sum(int(vals.get(k, 0)) for k in ("enodata", "exdev", "other")) / 10.0, 1)
    except (OSError, IndexError, ValueError):
        return None


def _data_free_mb():
    try:
        st = os.statvfs("/data")
        return int(st.f_bavail * st.f_frsize // (1024 * 1024))
    except OSError:
        return None


def _json_file(path):
    """A small status file written by another service, or None. Never raises."""
    try:
        with open(path) as f:
            return json.loads(f.read(4096))
    except Exception:
        return None

def _udc_state():
    udcdir = "/sys/class/udc"
    if os.path.isdir(udcdir):
        for f in os.listdir(udcdir):
            return read("%s/%s/state" % (udcdir, f))
    return ""

_PIDS = {}

def _pid_for(pattern):
    """First pid whose command line contains `pattern`. Remembered and re-validated against
    /proc/<pid>/cmdline, so pgrep only runs when the process has actually been replaced."""
    pid = _PIDS.get(pattern)
    if pid:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                if pattern.encode() in f.read().replace(b"\0", b" "):
                    return pid
        except Exception:
            pass
    pids = sh(["pgrep", "-f", pattern]).split()
    _PIDS[pattern] = pids[0] if pids else None
    return _PIDS[pattern]

def _feeder_cpu_ticks():
    """(pid, utime+stime clock ticks) of the net video feeder, or (None, None)."""
    pid = _pid_for("udpsrc port=5000")
    pids = [pid] if pid else []
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
    pid = _pid_for("udpsrc port=5002")
    pids = [pid] if pid else []
    if not pids:
        return None, None
    stat = read("/proc/%s/stat" % pids[0])
    try:
        f = stat.rsplit(")", 1)[1].split()
        return pids[0], int(f[11]) + int(f[12])
    except Exception:
        return pids[0], None


# Remembered between /api/status calls so liveness can be a DELTA rather than a guess.
# {name: (counter, monotonic_at_read)}
_STREAM_SEEN = {}


def _stream_live(name, value, now):
    """True only if this stream's counter has MOVED since the previous status poll.

    WHY NOT "is the service active"
    -------------------------------
    That was the old test, and it could not be false. bridge-feeder-net, -audio and
    -return-audio are enabled at boot and sit on their UDP sockets forever, so they read
    `active` on an idle bridge with nobody connected to it. Combined with "a USB cable is
    plugged in", the fleet reported a bridge as LIVE whenever it was merely powered — which
    is what it showed on 2026-08-24 with the presenter app stopped and no encoder running
    anywhere. An indicator that is true whenever the device has power is not an indicator.

    A counter that advances is evidence: CPU burned by a feeder means RTP is actually being
    decoded, and a capture hw_ptr that moves means the meeting laptop is genuinely delivering
    audio. Both are already read for /api/checks; here they are compared against the previous
    poll instead of sampled over a sleep, so /api/status stays cheap enough for a 5s panel.

    The first poll after a restart has nothing to compare against and reports False. That is
    the right way round: a stream shows up one poll late rather than a dead one showing up
    as live forever.
    """
    prev = _STREAM_SEEN.get(name)
    _STREAM_SEEN[name] = (value, now)
    if value is None or prev is None:
        return False
    old, then = prev
    if old is None:
        return False
    gap = now - then
    # A stale cache (process asleep, panel closed for an hour) says nothing about NOW.
    if gap <= 0 or gap > 120:
        return False

    # ANY advance is not enough. An idle GStreamer feeder still wakes up now and then, so
    # `value > old` made the voice flag flicker true/false for minutes after a session ended
    # - on a bridge with nothing arriving at it. A rate floor separates the two cleanly:
    # decoding real RTP burns ~14 CPU ticks/second (29 ticks over a 2s sample, measured on
    # this hardware), while an idle feeder manages roughly 0.1. One tick per second sits two
    # orders of magnitude below live and an order above idle.
    #
    # The return stream is exempt: its counter is a CAPTURE pointer in frames, which advances
    # at the sample rate (48000/s) whenever the meeting laptop is playing into the bridge -
    # something that continues perfectly correctly after the presenter has gone home.
    rate = (value - old) / gap
    floor = 1.0 if name in ("video", "voice") else 0.0
    return rate > floor


def _return_pcm():
    """The capture ring's pointers — the measurement that separates the two candidate causes
    of our missing audio, and which nothing currently exposes.

        avail / avail_max   how full the ring gets. If the host outruns us, this CLIMBS
                            toward the buffer size and the overflow is discarded: a rate
                            mismatch, which the pitch controller fixes.
        hw_ptr              frames the host actually delivered. If this falls behind wall
                            clock while avail stays at one period, frames never arrived at
                            all: missed USB transfers (raspberrypi/linux#5188), which no
                            feedback loop can repair.

    Both produce "missing audio, ALSA silent". They need opposite work, so the numbers have
    to be visible over minutes, not inferred from a two-second snapshot.
    """
    st = read(PCM_RETURN_STATUS)
    if not st or st.startswith("closed"):
        return None
    out = {}
    for ln in st.splitlines():
        for key in ("state", "avail_max", "avail", "hw_ptr", "appl_ptr", "delay"):
            if ln.startswith(key):
                v = ln.split(":", 1)[1].strip() if ":" in ln else ""
                out[key] = v if key == "state" else (int(v.split()[0]) if v.split() and v.split()[0].isdigit() else None)
                break
    return out or None


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
        # CPU says the feeder is BUSY. Frames per second says the room is getting a PICTURE -
        # a stream collapsed to a couple of frames a second burns CPU exactly like a healthy
        # one, so "arriving" and "watchable" were previously the same claim.
        fps = video_throughput(pid, time.monotonic())
        want = _expected_fps()
        if fps is None:
            video_detail = ("feeder pid %s used %d cpu ticks in %.0fs "
                            "(frame rate needs a second poll)" % (pid, dt, win))
        else:
            video_detail = ("feeder pid %s: %.1f fps to the gadget (expected ~%d), "
                            "%d cpu ticks in %.0fs" % (pid, fps, want, dt, win))
            # Two thirds of nominal is the line between "someone would call this broken" and
            # "a codec having a hard second". Below it the picture is visibly stuttering.
            if fps < want * 0.66:
                video_ok = False
                video_detail += " — DEGRADED: the room is seeing a stuttering picture"

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
    pin = pin_state()
    locked = bool(pin) and pin.get("protocol", 0) >= PIN_PROTOCOL and not (pin.get("session") or {}).get("active")
    if locked and not video_ok:
        video_detail = ("the bridge is LOCKED - no PIN session is open, so it refuses video. "
                        "Enter the PIN in the app (%s)" % ((pin.get("last_end") or {}).get("reason") or "not unlocked"))
    return {
        # Say so where the operator is actually looking. The app polls /api/checks every 10s
        # and shows these lines; putting "still settling" only in /api/status would leave the
        # person hearing the bursts with no explanation for them.
        "online": {"ok": True, "detail": (
            "bridge-web serving on :%d — STILL SETTLING after a flash "
            "(expanding the filesystem and generating host keys). Audio may burst for a "
            "minute or two; this finishes on its own." % PORT) if settling() else
            "bridge-web serving on :%d" % PORT},
        "video_arriving": {"ok": video_ok, "detail": video_detail},
        "voice_arriving": {"ok": voice_ok, "detail": voice_detail},
        "client_sees_camera": {"ok": udc == "configured",
                               "detail": "usb gadget state: %s" % (udc or "?")},
        "return_audio": {"ok": audio_ok, "detail": audio_detail},
        # Tuning the fleet wants the PRESENTER APP to apply. Carried here because /api/checks
        # is the one thing the app already polls on a timer; see presenter_tuning().
        "presenter_tuning": presenter_tuning(),
        # The app reads this to ask for the PIN again when the bridge relocked itself (Stop,
        # 10 min without video, 12 h, an admin lock, a reboot, or another presenter's PIN).
        "pin": {"locked": locked, "lockout": bool(pin.get("lockout")),
                "last_end": (pin.get("last_end") or {}).get("reason"),
                "protocol": pin.get("protocol", 1)},
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
    def _send(self, body, ctype, status=200):
        # status defaults to 200 so every existing caller is unchanged; a refusal needs to be
        # a real 403, not a 200 carrying {"ok": false}, or no client or proxy can tell the
        # difference between "denied" and "worked".
        self.send_response(status)
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
        elif path == "/api/return-tune":
            # Read back what the receiver will actually source. Never execute config
            # contents in the web process. New writable config overrides legacy fields.
            values = {"props": "", "pre": ""}
            source = None
            for config in ("/etc/default/bridge-return-tune", "/data/config/bridge-return-tune"):
                if not os.path.isfile(config):
                    continue
                source = config
                for line in read(config).splitlines():
                    match = re.fullmatch(r'\s*(RETURN_SRC_PROPS|RETURN_PRE_RESAMPLE)="([a-zA-Z0-9=_.! -]*)"\s*', line)
                    if match:
                        values["props" if match[1] == "RETURN_SRC_PROPS" else "pre"] = match[2]
            self._send(json.dumps(dict(values, source=source, writable_path="/data/config/bridge-return-tune")).encode(),
                       "application/json; charset=utf-8")
        elif path == "/api/lock-state":
            # Is a PIN session open, is the bridge locked out, is the media gate armed? The app
            # checks "protocol" here BEFORE unlocking: on an older bridge unlock restarted media.
            obj = pin_state() or {"pin_set": False, "locked": True, "lockout": False,
                                  "lockout_remaining": 0, "protocol": 1}
            self._send(json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(page().encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # Every POST on this server mutates something. None of them has a legitimate caller on
        # the LAN, so the check sits here rather than being repeated per endpoint - a new
        # mutating endpoint added later is protected by default instead of by remembering.
        peer = (self.client_address or ("",))[0]
        if not _mesh_or_local(peer):
            _audit(path, peer, False)
            self._send(json.dumps({"ok": False, "error": "forbidden",
                                   "detail": "this endpoint is reachable from the mesh only"}
                                  ).encode("utf-8"),
                       "application/json; charset=utf-8", status=403)
            return
        _audit(path, peer, True)
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("unsupported transfer encoding")
            lengths = self.headers.get_all("Content-Length") if hasattr(self.headers,"get_all") else [self.headers.get("Content-Length","0")]
            if lengths and len(lengths) != 1: raise ValueError("ambiguous content length")
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n < 0 or n > 16384:
                self._send(b'{"ok":false,"error":"request too large"}', "application/json", status=413)
                return
            body = json.loads(self.rfile.read(n) if n else b"{}")
            if not isinstance(body, dict): raise ValueError("object required")
        except Exception:
            self._send(b'{"ok":false,"error":"invalid request body"}', "application/json", status=400)
            return
        caller = _caller_ip(peer)
        if path in ("/api/set-peer", "/api/return-tune") and not self._session_ok(caller, body):
            return
        if path == "/api/set-peer":
            # Register the presenter as the return-audio destination — the SSH-free
            # replacement for `ssh pi@bridge bridge set-peer <ip>`. Strictly a
            # dotted-quad (we only ever point at a mesh IP), and idempotent: if the
            # peer is already this ip:port we skip the return-audio restart so
            # re-going-live never blips the meeting audio.
            try:
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
            if not _is_local(caller) and ip != caller:
                # The presenter app always names itself; a ticket is not permission to send the
                # room's microphone to a third address (2026-09-25 audit).
                self._send(json.dumps({"ok": False, "error": "forbidden",
                                       "detail": "the return destination must be the caller's own mesh address"}
                                      ).encode("utf-8"), "application/json; charset=utf-8", status=403)
                return
            cur = read("/etc/default/bridge-return-audio")
            current = {}
            for line in cur.splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() in ("RETURN_DEST_IP", "RETURN_DEST_PORT"):
                    current[key.strip()] = value.strip().strip("\"'")
            if current.get("RETURN_DEST_IP") == ip and current.get("RETURN_DEST_PORT") == port:
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
            error = None
            try:
                r = subprocess.run(["sudo", "-n", "/usr/local/bin/bridge"] + args,
                                   capture_output=True, text=True, timeout=25)
                ok = r.returncode == 0
                if not ok:
                    error = (r.stderr or r.stdout or "return-tune failed")[-2000:].strip()
            except Exception as e:
                ok = False
                error = "return-tune failed: %s" % type(e).__name__
            self._send(json.dumps({"ok": ok, "clear": clear, "props": props,
                                   "pre": pre, "error": error}).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/unlock":
            # The PIN is verified ON THE DEVICE ITSELF - never forwarded to the control plane,
            # never logged, never on a command line. Presenter app -> this bridge over the mesh.
            # A correct PIN opens the session for the CALLER's mesh address (where its media will
            # come from). A tool on the bridge itself (loopback) may name the presenter instead.
            local = _is_local(caller)
            who = str(body.get("peer") or "") if local else caller
            args = ["unlock", "-"] + (["--peer", who] if who else [])
            code, j = _pin_run(args, str(body.get("pin", "")))
            reason = j.get("reason") or {0: "ok", 1: "wrong", 2: "locked_out_now", 3: "locked_out",
                                         5: "no_pin", 6: "bad_format"}.get(code, "error")
            resp = {"ok": code == 0 and bool(j.get("ticket")), "reason": reason,
                    "message": j.get("message") or ("unlock failed on device" if code == 4 else ""),
                    "protocol": PIN_PROTOCOL}
            # Only an app that asks for protocol 2 receives the ticket. Older apps display this
            # whole answer on screen, and a ticket read off a shared screen opens the gate for
            # whoever copies it (2026-09-25 audit). They unlock without it; the PIN still counted.
            try:
                wants_ticket = int(body.get("protocol") or 1) >= PIN_PROTOCOL
            except (TypeError, ValueError, OverflowError):     # "protocol": Infinity is valid JSON
                wants_ticket = False
            for k in ("ticket", "peer", "gate", "expires_in", "idle_timeout", "superseded",
                      "attempt", "attempts_left", "retry_in"):
                if k in j and (k != "ticket" or wants_ticket):
                    resp[k] = j[k]
            if code == 0 and not wants_ticket:
                resp["ok"] = True
            self._send(json.dumps(resp).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/end-session":
            # Stop in the app. Ends THIS ticket's session and closes the media gate; a stale
            # ticket cannot end someone else's. Nothing is restarted.
            code, j = _pin_run(["end", "-"], str(body.get("ticket", "")))
            self._send(json.dumps({"ok": code == 0 and bool(j.get("ok")), "ended": bool(j.get("ended")),
                                   "message": j.get("message", "")}).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(json.dumps({"ok": False, "error": "not found"}).encode("utf-8"),
                       "application/json; charset=utf-8")

    def _session_ok(self, caller, body):
        """Go-live actions need the open session's ticket. Refused = HTTP 401 with the reason,
        so the app knows to ask for the PIN (and an old app without tickets is refused plainly).
        The session follows the caller's mesh address: the app's helper may restart mid-session."""
        args = ["check", "-", "--rebind"] + (["--peer", caller] if caller and not _is_local(caller) else [])
        code, j = _pin_run(args, str(body.get("ticket", "")))
        if code == 0:
            return True
        reason = j.get("reason") or "locked"
        self._send(json.dumps({"ok": False, "error": "locked", "reason": reason,
                               "detail": j.get("message") or "the bridge is locked - enter the PIN to go live",
                               "protocol": PIN_PROTOCOL}).encode("utf-8"),
                   "application/json; charset=utf-8", status=401)
        return False

    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    address_family = socket.AF_INET6
    daemon_threads = True
    max_workers = 16
    socket_timeout = 10

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_workers)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.socket_timeout)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()

if __name__ == "__main__":
    Server(("::", PORT), H).serve_forever()
