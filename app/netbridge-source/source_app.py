#!/usr/bin/env python3
"""NetBridge Source — the presenter app (walkthrough Journey 3).

Runs entirely on the presenter's machine and serves its UI on 127.0.0.1 only. It is the
thing that turns J3's six steps into software:

  1 install                 -> this app (packaging/signing is M7, needs an Apple account)
  2 sign in with work email -> magic link against the control plane, personal token
  3 pick bridge/camera/mic  -> real dropdowns, remembered BY NAME (ledger E4), never by
                               device index: avfoundation renumbers when you plug in a
                               headset, so a saved index silently streams the wrong camera
  4 the PIN, every go-live  -> verified ON THE DEVICE (/api/unlock), which answers with a
                               one-session ticket kept in this process's memory only. Seeing a
                               bridge in the list is not permission to stream to it
  5 go live, four checks    -> spawns the same ffmpeg legs as mac-stream.sh and polls the
                               device's /api/checks, which measures real activity
  6 hear the room back      -> registers THIS machine as the return-audio peer through the
                               control plane (never SSH) and plays the return stream

Deliberately NOT in this app: any admin capability. It signs in as a viewer, so the
control plane refuses fleet mutations even if the UI asked for them.
"""
import hashlib, json, os, pathlib, re, shutil, socket, subprocess, sys, threading, time
import urllib.request, urllib.error, urllib.parse
import math


def _ensure_ca_bundle():
    """Give the frozen app a CA store before anything opens HTTPS.

    Running from source, Python finds the OS root certificates and TLS to the fleet just
    works. A PyInstaller build does not: it carries its own Python and OpenSSL with no
    linked cert store, so the FIRST https call dies with

        [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

    and the failure surfaces as an empty BRIDGE dropdown and a dead Go live button — which
    reads as "the app is broken", not "the app cannot verify a certificate". Source builds
    were unaffected, so this only ever bit the packaged app.

    certifi ships the Mozilla roots and PyInstaller bundles its cacert.pem. Publishing the
    path through the environment covers urllib, ssl and anything else that consults
    OpenSSL's default paths. An operator-set SSL_CERT_FILE always wins, for corporate roots.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
        ca = certifi.where()
    except Exception:
        return                                   # source run: the OS store is already fine
    if ca and os.path.exists(ca):
        os.environ["SSL_CERT_FILE"] = ca
        os.environ["REQUESTS_CA_BUNDLE"] = ca


_ensure_ca_bundle()

STATE_DIR = pathlib.Path(os.path.expanduser("~/.netbridge-source"))
STATE_FILE = STATE_DIR / "state.json"
HOST, PORT = "127.0.0.1", 8765

# Ports the bridge listens on (bridge-feeder-net / bridge-feeder-audio).
RTP_VIDEO, RTP_VOICE = 5000, 5002

# Build stamp. build.py rewrites this line, and it is what the updater compares against
# the signed manifest — so a build that forgets to bump it simply never updates, rather
# than update-looping.
APP_VERSION = "1.5.1"


# --------------------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(STATE_FILE)
    try:
        STATE_FILE.chmod(0o600)   # holds the personal bearer token
    except Exception:
        pass


# ------------------------------------------------------------------------ updates
# Walkthrough J3: "One signed desktop app, kept current by auto-update" / "Updated to
# 2.3.1 while you were away". Trust model is deliberately the SAME one the image OTA
# already uses (bridge-update.sh): an EC-signed manifest naming a sha256, verified
# against a pinned public key that ships inside the app. Apple's Developer ID is about
# Gatekeeper letting the app RUN; it does not authenticate an update channel, so this
# works with or without it.
#
# Two rules make it safe to leave on:
#   * NEVER swap a running binary mid-session. The download stages a file next to the
#     app; the swap happens at the NEXT start, before anything binds a port. That is
#     literally "updated while you were away" — a live meeting can never be interrupted.
#   * FAIL CLOSED. No pubkey, no openssl, bad signature, wrong hash -> no update.
UPDATE_MANIFEST = "manifest.txt"
UPDATE_CHECK_S = 6 * 3600      # re-check while running; the swap still waits for a restart
_update_note = None            # set once an update has been staged/applied, shown in the UI


def _app_binary():
    """Only a standalone packaged executable can use the binary update channel.

    Replacing Contents/MacOS inside a .app invalidates its bundle signature and
    permission identity. Native bundles must be replaced as complete packages.
    """
    if not getattr(sys, "frozen", False): return None
    exe = pathlib.Path(sys.executable).resolve()
    if any(parent.suffix.lower() == '.app' for parent in exe.parents): return None
    return exe


def _update_pubkey():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "app-pubkey.pem"),
              os.path.join(os.path.dirname(str(_app_binary() or "")), "app-pubkey.pem")):
        if p and os.path.exists(p):
            return p
    return None


def _verify_sig(pubkey, sig_path, data_path):
    """Verify with bundled crypto on Windows/macOS; source runs may use OpenSSL."""
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        if not shutil.which("openssl"): return False
        try:
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pubkey),
                                "-signature", str(sig_path), str(data_path)], timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired): return False
    try:
        public = serialization.load_pem_public_key(pathlib.Path(pubkey).read_bytes())
        if not isinstance(public, ec.EllipticCurvePublicKey): return False
        public.verify(pathlib.Path(sig_path).read_bytes(), pathlib.Path(data_path).read_bytes(),
                      ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _update_compatible(fields):
    # Separate feed prevents older clients from discovering this protocol-2 release.
    # Missing metadata/fleet evidence is unknown, not permission to migrate.
    if fields.get('platform') != _plat_tag() or fields.get('bridge_protocol') != '2': return False
    # A binary-only update must not pair a new app with an unrelated old helper.
    # Missing/different helper evidence requires the complete installer.
    try:
        helper = _mesh_bin()
        if not helper or _sha256(helper) != fields.get('helper_sha256'): return False
    except OSError:
        return False
    st = load_state()
    if not st.get('bridge_id') or not st.get('token'): return False
    try:
        bridges = _fleet_bridges(st)
        return any(b.get('id') == st['bridge_id'] and b.get('online') is True and
                   b.get('pin_protocol') == 2 for b in bridges if isinstance(b, dict))
    except Exception:
        return False


def _manifest_fields(path):
    """Bounded, unambiguous signed metadata. Filenames never escape staging."""
    try:
        raw = pathlib.Path(path).read_bytes()
        if len(raw) > 16384: return None
        fields = {}
        for line in raw.decode('utf-8').splitlines():
            if not line or line.startswith('#'): continue
            key, value = line.split('=', 1)
            if key in fields: return None
            fields[key] = value
        if not re.fullmatch(r'[0-9a-f]{64}', fields.get('sha256','')): return None
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',fields.get('file','')): return None
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?',fields.get('version','')): return None
        return fields
    except (OSError, ValueError, UnicodeError):
        return None


def apply_staged_update():
    """Swap in a previously staged update. Runs FIRST at startup, before any port is
    bound, so the app can replace itself with nothing in flight. Keeps the outgoing
    binary as .old — if the new one cannot start, that file is the way back."""
    global _update_note
    exe = _app_binary()
    if not exe:
        return
    staged, meta = exe.with_suffix(".new"), exe.with_suffix(".new.json")
    man, sig = exe.with_suffix(".new.manifest"), exe.with_suffix(".new.sig")
    if not (staged.exists() and meta.exists()):
        return
    swapped = False
    try:
        pub = _update_pubkey()
        if not pub or not man.exists() or not sig.exists() or not _verify_sig(pub, str(sig), str(man)):
            _update_note = "Staged update could not be authenticated. Keeping the current app."
            return
        info = _manifest_fields(man)
        if not info or not _update_compatible(info):
            _update_note = "Update waits until compatibility with the selected bridge can be verified."
            return
        # Only the signed manifest authorizes bytes, never the editable staging JSON.
        if _sha256(str(staged)) != info.get("sha256"):
            staged.unlink(missing_ok=True); meta.unlink(missing_ok=True)
            return
        old = exe.with_suffix(".old")
        old.unlink(missing_ok=True)
        os.replace(str(exe), str(old))       # atomic; the running image stays mapped
        swapped = True
        os.replace(str(staged), str(exe))
        os.chmod(str(exe), 0o755)
        meta.unlink(missing_ok=True)
        save_state({**load_state(), "updated_to": info.get("version"),
                    "updated_from": APP_VERSION})
        # A new onefile build must extract its own runtime, not inherit the old _MEIPASS.
        env = {k:v for k,v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        os.execve(str(exe), [str(exe)] + sys.argv[1:], env)
    except Exception:
        # Restore the old executable if rename or launch failed. The old process is still
        # mapped and can continue; never leave its normal launch path missing or broken.
        try:
            if swapped and old.exists():
                os.replace(str(old), str(exe))
            _update_note = "Update installation failed; the previous app was restored."
        except OSError:
            _update_note = "Update installation failed. Restore the .old backup before the next launch."
        try:
            staged.unlink(missing_ok=True); meta.unlink(missing_ok=True)
        except Exception:
            pass


def _plat_tag():
    if IS_WIN:
        return "windows"
    return "macos-arm64" if (IS_MAC and os.uname().machine == "arm64") else \
           ("macos-x86_64" if IS_MAC else "linux")


def check_for_update(base_url):
    """Fetch + verify + stage. Returns the new version string, or None. Safe to call in a
    background thread; it never touches the running binary."""
    global _update_note
    exe = _app_binary()
    pub = _update_pubkey()
    if not (exe and base_url and pub):
        return None                      # dev run, or no pinned key -> updates disabled
    root = "%s/app/pin-v2/%s" % (base_url.rstrip("/"), _plat_tag())
    tmp = pathlib.Path(STATE_DIR) / "update"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        man, sig = tmp / UPDATE_MANIFEST, tmp / (UPDATE_MANIFEST + ".sig")
        urllib.request.urlretrieve("%s/%s" % (root, UPDATE_MANIFEST), man)
        urllib.request.urlretrieve("%s/%s.sig" % (root, UPDATE_MANIFEST), sig)
        if not _verify_sig(pub, str(sig), str(man)):
            return None                  # unsigned/tampered manifest: stop here
        fields = _manifest_fields(man)
        if not fields:
            return None
        if not _update_compatible(fields): return None
        ver, want, fname = fields['version'], fields['sha256'], fields['file']

        # IDENTITY IS THE DIGEST, NOT THE VERSION STRING.
        #
        # This used to read `or ver == APP_VERSION: return None`, and that one comparison made
        # the updater structurally unable to do its job. On 25 Aug 2026 three DIFFERENT binaries
        # were all stamped 1.1.9: the one running in production (Python 3.14, built locally), the
        # one published on GitHub (Python 3.12, built by CI), and a third from the same source.
        # Version strings here are hand-typed labels -- the image side has the same disease,
        # which is why a JULY image is labelled 2.0.1 while AUGUST images say 2.0.0.
        #
        # So a same-version-different-bytes update could never be delivered, and the app could
        # not even tell it was running something other than the published artifact.
        #
        # The manifest already carries a sha256. Compare THAT against the running binary: a
        # digest cannot be mistyped. The version string survives only for display and for the
        # "updated while you were away" note.
        try:
            running = _sha256(str(exe))
        except Exception:
            # Cannot hash ourselves -> cannot establish identity -> do not update.
            # Failing closed is the entire point of this path.
            return None
        if running == want:
            return None                     # byte-identical: genuinely nothing to do
        blob = tmp / fname
        urllib.request.urlretrieve("%s/%s" % (root, fname), blob)
        if _sha256(str(blob)) != want:
            return None                  # signed manifest, wrong bytes: refuse
        staged = exe.with_suffix(".new")
        shutil.move(str(blob), str(staged))
        shutil.copyfile(man, exe.with_suffix(".new.manifest"))
        shutil.copyfile(sig, exe.with_suffix(".new.sig"))
        exe.with_suffix(".new.json").write_text(
            json.dumps({"version": ver, "sha256": want}))
        _update_note = "Update %s ready — it installs next time you start the app." % ver
        return ver
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- http
def api(method, url, token=None, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode())
            out = {"_error": j.get("detail", str(e)), "_code": e.code}
            if isinstance(j, dict) and j.get("reason"):
                out["_reason"] = j["reason"]        # e.g. a bridge saying WHY it is locked
            return out
        except Exception:
            return {"_error": str(e), "_code": e.code}
    except Exception as e:
        return {"_error": str(e)}


def _signin_url(value):
    """Normalize a Fleet address before using or saving it."""
    value = str(value or "").strip().rstrip("/")
    if not value:
        raise ValueError("Enter your Fleet address.")
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Enter a valid Fleet address, such as https://fleet.example.com.")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Use HTTPS for your Fleet address.")
    try:
        parsed.port
    except ValueError:
        raise ValueError("The Fleet address has an invalid port.")
    return value


def _signin_error(result, redeem=False):
    code = (result or {}).get("_code")
    if code in (401, 403):
        return ("This code is invalid, expired or already used. Request a new code."
                if redeem else "Your sign-in is no longer valid. Sign out and sign in again.")
    if code == 429:
        return "Too many attempts. Wait a moment before trying again."
    if code is None:
        return "Cannot reach Fleet. Check your internet connection and Fleet address, then try again."
    return "Fleet could not complete this request. Try again or contact your fleet admin."


def _redeem_signin(url, code):
    result = api("POST", url + "/auth/magic-redeem", body={"code": code})
    # Only a rejected code can be an invite. An outage must not trigger a second
    # redemption attempt or hide its cause behind an invite error.
    if not (result or {}).get("token") and (result or {}).get("_code") == 401:
        result = api("POST", url + "/auth/redeem", body={"invite": code})
    return result


# --------------------------------------------------------------------------- platform
# macOS and Windows differ in every part of the capture path: the ffmpeg input format,
# how a device is addressed, and which h264 encoder exists. Keep those differences in ONE
# place so the rest of the app never branches on sys.platform.
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# avfoundation addresses devices by INDEX ("0:none"); dshow addresses them by NAME
# (video="Integrated Camera"). That is why the app stores the device NAME and resolves it
# at go-live: it is the only identifier both platforms share, and on macOS the index moves
# when you plug in a headset.
AV_FMT = "dshow" if IS_WIN else "avfoundation"

# WHAT THE MEETING ROOM ACTUALLY SEES.
#
# The USB gadget advertises ONE frame size -- 424x240 uncompressed at 30 fps since 2026-09-24
# (see pi/scripts/uvc-raw-setup.sh, create_frame ... 424 240 uncompressed; it was 640x360 @20
# from 17 Jul, and before that 320x180). Until 26 Aug 2026 this encoder produced 320x180 into the
# 640x360 gadget, so the bridge upscaled it - the two numbers had never been compared.
#
# Encode at the gadget's own size: no scaling on the host or the bridge, and the picture stops
# being the weakest part of the product.
#
# The ceiling is NOT the network, it is USB - and not its bandwidth but its packet mode. The Pi 4's
# OTG port is USB 2.0 high-speed; an isochronous endpoint gets one slot per 125 us microframe.
# 640x360 YUY2 @20fps (9.2 MB/s) did not fit in one 1024-byte packet per slot and needed
# "high-bandwidth" mode (two packets per slot), where the Pi's dwc2 controller missed slots and
# the meeting laptop saw choppy video in every app (2026-09-24: everything upstream measured
# clean). 424x240 @30 (6.1 MB/s, 75%) fits in ONE packet per slot; 480x270 @30 would need 95%.
# 30 fps because the Mac camera captures at 30: 20 does not divide 30, so keeping 2 frames of every
# 3 spaced them 33/67 ms apart - motion judder no network or USB fix could remove.
# Must equal the UVC frame in pi/scripts/uvc-raw-setup.sh (tests/test-artifact-identity.py checks it).
STREAM_W, STREAM_H = 424, 240
STREAM_FPS = 30

# Deliberately a fixed value: this encoder has no congestion feedback, so a "smart" bitrate here
# would be a guess wearing a suit. Adaptive rate control belongs behind real RTCP feedback.
# Measured 2026-09-24 through the bridge's own pipeline (H.264 Baseline, 20fps, no scaling):
#   640x360 @800k -> 0.21-0.22 s Pi CPU per 30 s, SSIM 0.972 / 0.957 (webcam-like / busy scene)
#   480x270 @600k -> 0.16 s,                      SSIM 0.961 / 0.941
#   480x270 @500k -> 0.14-0.15 s,                 SSIM 0.960 / 0.938
#   320x180 @400k -> 0.11 s,                      SSIM 0.940 / 0.917 (visibly soft)
#   424x240 @30fps 600k (from a 30 fps camera) -> 0.17 s, SSIM 0.953 / 0.931 per frame, 50% more
#   frames and no 30->20 cadence judder; 700k measured no visible gain (0.954 / 0.934)
STREAM_BITRATE = "600k"


def _is_exe(path):
    """True if path is a runnable binary. os.access(X_OK) is meaningless on Windows -
    a perfectly good bundled .exe can test False there, which would silently drop us back
    to a system ffmpeg that a presenter does not have. On Windows, existing is enough."""
    if not os.path.isfile(path):
        return False
    return True if IS_WIN else os.access(path, os.X_OK)


def _media_base():
    """Allow source runs to reuse a release's media tools without packaging Python."""
    return os.environ.get("NB_MEDIA_DIR") or getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def _ffmpeg():
    """Prefer an ffmpeg shipped next to this app, fall back to one on PATH.

    A packaged build bundles its own binary so a presenter installs nothing (walkthrough
    J3 step 1: "the app brings everything"). Running from source, the system one is fine.
    """
    base = _media_base()
    local = os.path.join(base, "ffmpeg.exe" if IS_WIN else "ffmpeg")
    if _is_exe(local):
        return local
    return shutil.which("ffmpeg") or "ffmpeg"


def _gst():
    """Path to gst-launch-1.0 — the bundled copy if we shipped one, else the system's."""
    base = _media_base()
    for cand in (os.path.join(base, "gst", "gst-launch-1.0.exe" if IS_WIN else "gst-launch-1.0"),
                 os.path.join(base, "gst-launch-1.0.exe" if IS_WIN else "gst-launch-1.0")):
        if _is_exe(cand):
            return cand
    return shutil.which("gst-launch-1.0")


def _gst_env():
    """Environment for the bundled GStreamer.

    A relocated GStreamer cannot find its own plugins: the registry paths are baked in at
    ITS build time and point at wherever it was compiled. Without these two variables the
    binary starts fine and then fails with "no element udpsrc", which reads like a broken
    install rather than a missing search path.
    """
    env = dict(os.environ)
    base = _media_base()
    gstdir = os.path.join(base, "gst")
    plug = os.path.join(gstdir, "plugins")
    if os.path.isdir(plug):
        env["GST_PLUGIN_PATH"] = plug
        env["GST_PLUGIN_SYSTEM_PATH_1_0"] = plug
        # a stale registry from another install would shadow the bundle
        env["GST_REGISTRY"] = os.path.join(str(_logdir()), "gst-registry.bin")
        if IS_WIN:
            # Windows finds a DLL's dependencies via PATH (and the exe's own dir). The
            # bundled gst-launch and its DLLs share gst/, so that dir must lead PATH or
            # the binary starts and immediately fails to load libglib etc.
            env["PATH"] = gstdir + os.pathsep + env.get("PATH", "")
    return env


def _logdir():
    """Windows has no /tmp. Keep leg logs beside the app's state instead."""
    d = STATE_DIR / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return pathlib.Path(os.environ.get("TEMP", ".")) if IS_WIN else pathlib.Path("/tmp")
    return d


def _open_browser(url):
    try:
        if IS_WIN:
            os.startfile(url)                                    # noqa: S606
        elif IS_MAC:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# --------------------------------------------------------------------------- devices
def av_devices(timeout=25):
    """Enumerate cameras and mics BY NAME.

    Both platforms print their device list to STDERR from a deliberately-failing probe
    command, but in different shapes:
      avfoundation:  [AVFoundation indev @ ...] [0] FaceTime HD Camera
      dshow:         [dshow @ ...]  "Integrated Camera" (video)
    Names are what we persist, so the two are normalised to the same {index,name} pairs.
    """
    ff = _ffmpeg()
    if not ff:
        return {"video": [], "audio": [], "error": "ffmpeg not found"}
    args = ([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
            if IS_WIN else
            [ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return {"video": [], "audio": [], "error": str(e)}
    err = p.stderr or ""

    video, audio = [], []
    if IS_WIN:
        # dshow prints:  "Name" (video)   /   "Name" (audio)
        for line in err.splitlines():
            m = re.search(r'"([^"]+)"\s*\((video|audio)\)', line)
            if m:
                (video if m.group(2) == "video" else audio).append(
                    {"index": m.group(1), "name": m.group(1)})
    else:
        section = None
        for line in err.splitlines():
            low = line.lower()
            if "video devices" in low:
                section = "v"; continue
            if "audio devices" in low:
                section = "a"; continue
            if "] [" in line and section:
                try:
                    idx = line.split("] [")[1].split("]")[0]
                    name = line.split("] ", 2)[-1].strip()
                    (video if section == "v" else audio).append({"index": idx, "name": name})
                except Exception:
                    pass
    return {"video": video, "audio": audio}


def resolve_by_name(devs, saved_name, fallback_index="0"):
    """Map a REMEMBERED NAME back to today's index. avfoundation reorders devices when
    you plug in a headset or a second display, so persisting the index is what makes an
    app stream the wrong camera after a reboot (ledger E4)."""
    if saved_name:
        for d in devs:
            if d["name"] == saved_name:
                return d["index"], d["name"], True
    if devs:
        return devs[0]["index"], devs[0]["name"], False
    return fallback_index, "", False


def local_ip_towards(host):
    """The address THIS machine has on the route to the bridge — what the bridge should
    send return audio to. Guessing 127.0.0.1 or the first NIC breaks on multi-homed
    machines and on the mesh."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def bridge_reachable(host, port=8080, timeout=1.0):
    """Is this bridge answering directly right now (i.e. on our own LAN)? A fast TCP
    probe so the app can prefer the direct path over the mesh whenever the bridge is
    reachable — instead of always trying the mesh first and hanging on its handshake
    when the tailnet is blocked. Walkthrough J3: the app picks the reachable path itself."""
    if not host:
        return False
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- session
def _locked(fn):
    """Serialise one Session mutation against every other.

    Applied to start/stop/respawn_leg/set_return — the four methods that touch the leg
    tables. Without it, a request handler and a supervisor tick can interleave and orphan
    an encoder process that still holds the camera.
    """
    def wrap(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrap.__name__, wrap.__doc__ = fn.__name__, fn.__doc__
    return wrap


class WaitingMicrophone:
    """A stopped voice leg awaiting its explicitly selected USB input."""
    pid = None
    stdin = None

    def poll(self):
        return 1


def bind_video_ticket(argv, ticket):
    """Non-secret RTP discriminator; supports old signed and new unsigned FFmpeg options."""
    argv = list(argv)
    if not ticket:
        return argv
    value = str((int(hashlib.sha256(ticket.encode()).hexdigest()[:8], 16) & 0x7fffffff) or 1)
    if "-ssrc" in argv:
        argv[argv.index("-ssrc") + 1] = value
    else:
        argv[-1:-1] = ["-ssrc", value]
    return argv


class VideoQuality:
    LEVELS = ((30, '600k'), (20, '400k'), (15, '250k'))

    def __init__(self):
        self.level = 0
        self.bad = 0
        self.good_since = None
        self.changed_at = None

    def observe(self, fps, now):
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
            self.bad = 0
            self.good_since = None
            return None  # no measurements/complete outage is not congestion evidence
        expected = self.LEVELS[self.level][0]
        if fps < expected * .65:
            self.bad += 1
            self.good_since = None
        elif fps >= expected * .9:
            self.bad = 0
            if self.good_since is None:
                self.good_since = now
        else:
            self.bad = 0
            self.good_since = None
        if self.changed_at is not None and now - self.changed_at < 60:
            return None
        next_level = self.level
        if self.bad >= 3 and self.level < len(self.LEVELS)-1:
            next_level += 1
        elif self.level and self.good_since is not None and now - self.good_since >= 180:
            next_level -= 1
        if next_level == self.level:
            return None
        self.level = next_level
        self.changed_at = now
        self.bad = 0
        self.good_since = None
        return self.LEVELS[self.level]


class Session:
    """Owns the live ffmpeg legs + the return listener."""

    def __init__(self):
        # ONE lock for every mutation of this object.
        #
        # The HTTP server is a ThreadingHTTPServer, and three separate writers touch the
        # leg tables: the request handler (go live / stop), StreamGuard's 5s tick, and
        # BridgeWatch's 10s tick. Session had no locking at all, so a double-click on Go
        # live ran start() twice: both called stop(), both spawned encoders, and the second
        # overwrote self.leg_proc — leaving the first pair ORPHANED. They keep holding the
        # camera and sending RTP, invisible to leg_status() and never reaped. An orphaned
        # encoder holding the camera is exactly what wedged the capture daemons on 10 Aug.
        #
        # Reentrant because start() calls stop() on the way in.
        self._lock = threading.RLock()
        self.procs = []
        self.logs = []
        self.bridge = None
        self.return_port = 5004
        self.return_player = "none"
        self.voice_proc = None
        self.video_quality = VideoQuality()
        self.capture_names = {}
        self.waiting_for_devices = {}
        self.voice_muted = False
        self.return_proc = None     # the return-audio player, tracked so it can be toggled
        self.return_on = True
        # Return-audio tunables, seeded from env and changeable mid-session (see
        # set_return_tuning). Kept on the session, not read from os.environ at each start,
        # so a presenter can correct pumping or jitter without quitting a live meeting.
        self.return_gain = os.environ.get("NB_RETURN_GAIN", "1.0" if IS_MAC else "2.0")
        self.return_jitter_ms = os.environ.get("NB_RETURN_JITTER_MS", "250")
        self.return_jitter_manual = False
        self.return_clock_mode = "slave"  # experimental alternative is opt-in only
        # Mac listening trial, 2026-09-16: synchronization helped, then bypassing
        # compression and the 2x boost helped further. Chipping remains unresolved;
        # these defaults preserve that improvement, not a proven clock diagnosis.
        # Other platforms retain their existing defaults. All three remain overridable.
        self.return_dynamics = os.environ.get("NB_RETURN_DYNAMICS", "0" if IS_MAC else "1") != "0"
        self.return_sink_sync = os.environ.get("NB_RETURN_SINK_SYNC", "1" if IS_MAC else "0") != "0"
        # Loss concealment: opusdec use-inband-fec + plc, and rtpjitterbuffer do-lost.
        #
        # DEFAULT OFF, and that default is the whole point of this line. The field-proven
        # recipe (338c578, "port the field-proven return-audio recipe — kills the jitter")
        # is explicit: "plc=true + inband-fec + do-lost SYNTHESIZE audio to conceal loss —
        # tested cleaner WITHOUT it (PLC's guesses are the artifacts). Use plain opusdec."
        #
        # When this pipeline was made tunable on 2026-08-03 the knob was added defaulting
        # ON, which quietly reverted that fix. Every fresh launch then started in the
        # configuration July had already proven worse, and the jitter came back — costing
        # days of hunting the bridge, the network, the mesh and the encoder, all of which
        # were healthy. A default that contradicts a proven result is not a default, it is
        # a regression with a switch on it.
        #
        # It is still switchable, because the trade genuinely reverses on a LOSSY link:
        # the bridge encodes with inband-fec=true and packet-loss-percentage=20, so the
        # redundancy is already on the wire and FEC reconstructs real audio rather than
        # guessing. On our link, measured loss is zero — so concealment can only invent.
        self.return_conceal = os.environ.get("NB_RETURN_CONCEAL", "0") != "0"
        # Defaults chosen from what the bridge actually sends: it encodes with inband-fec and
        # packet-loss-percentage=20, so the redundancy exists whether or not we use it. FEC on
        # costs nothing extra on the wire and repairs real loss. PLC stays off - its guesses
        # are the warble that was chased for a week.
        self.return_fec = os.environ.get("NB_RETURN_FEC", "1") != "0"
        self.return_plc = os.environ.get("NB_RETURN_PLC", "0") != "0"

    # Did the OPERATOR ask for this session, or is it merely still twitching?
    #
    # `live` below is derived purely from whether processes happen to be running, which means
    # a stopped session and a crashed one look identical to every supervisor in this file.
    # They then did what they exist to do and restarted the legs - so /api/stop killed the
    # encoders, the guard brought them back within seconds, and the camera light came back on.
    # On 2026-08-24 an operator ended a session and the camera kept recording; there was no
    # way to stop it short of quitting the app.
    #
    # Intent is not derivable from process state. It has to be recorded.
    wanted = False

    @property
    def live(self):
        return any(p.poll() is None for p in self.procs)

    def voice_sending(self):
        """True if the mic->bridge leg is alive. A sender-side signal used only as a
        fallback when the bridge firmware predates the device-side voice_arriving check."""
        return bool(self.voice_proc and self.voice_proc.poll() is None)

    @_locked
    def start(self, pi_host, video_idx, audio_idx, fps=STREAM_FPS, mic_gain=0, return_port=5004, mic_name=None):
        if _SUSPENDING.is_set():
            raise RuntimeError("Wait for the computer to finish waking before presenting.")
        self.stop()
        self.interruption = None
        self.video_quality = VideoQuality()
        self.capture_names = {}
        self.waiting_for_devices = {}
        self.bridge, self.return_port = pi_host, return_port
        ff = _ffmpeg()
        common = [ff, "-hide_banner", "-loglevel", "warning"]
        # The macOS arguments are mac/mac-stream.sh's PROVEN ones and must not be
        # "simplified": asking the camera for 320x180 or 640x360 fails outright
        # ("Selected video size is not supported by the device") and the video leg dies
        # instantly while the audio leg keeps running - which reads as a network fault
        # rather than a bad argument. Capture large, scale down.
        # -bsf:v dump_extra=freq=keyframe repeats SPS/PPS so a receiver joining late can
        # decode. Opus -fec is deliberately absent: ffmpeg's RTP muxer rejects it.
        if IS_WIN:
            # dshow takes device NAMES, and there is no videotoolbox: libx264 ultrafast
            # is the portable choice that every Windows ffmpeg build has.
            vin = ["-f", "dshow", "-rtbufsize", "64M", "-i", "video=%s" % video_idx]
            venc = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
            ain = ["-f", "dshow", "-i", "audio=%s" % audio_idx]
        else:
            vin = ["-f", "avfoundation", "-framerate", "30",
                   "-video_size", "1280x720", "-pixel_format", "uyvy422",
                   "-i", "%s:none" % video_idx]
            # Baseline profile: CAVLC instead of the default High profile's CABAC. CABAC is the most
            # expensive, strictly serial part of H.264 decoding, and the bridge decodes this stream
            # in software on a 900 MHz Pi. Measured 2026-09-22 through the bridge's own pipeline at
            # 640x360/20fps/1500k: 41% less decode+convert CPU, SSIM 0.966 vs 0.967 (not visible).
            # (The Windows path is already CAVLC: libx264 ultrafast turns CABAC off.)
            venc = ["-c:v", "h264_videotoolbox", "-realtime", "1", "-profile:v", "baseline"]
            ain = ["-f", "avfoundation", "-i", ":%s" % audio_idx]

        v = common + vin + [
            "-vf", "scale=%d:%d,format=nv12" % (STREAM_W, STREAM_H),
            "-fps_mode", "cfr", "-r", str(fps),
        ] + venc + [
            "-b:v", STREAM_BITRATE, "-g", str(fps), "-bsf:v", "dump_extra=freq=keyframe", "-an",
            "-f", "rtp", "rtp://%s:%d?pkt_size=1100" % (pi_host, RTP_VIDEO)]
        v = bind_video_ticket(v, PINS.ticket())
        a = common + ain + [
            # The Pi owns voice AGC. Avoid stacked boosts; disable the limiter's
            # automatic makeup gain so this safety ceiling really stays at 0.9.
            "-af", "volume=%ddB,alimiter=limit=0.9:level=false" % mic_gain,
            "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "2",
            "-application", "lowdelay", "-payload_type", "97",
            "-f", "rtp", "rtp://%s:%d" % (pi_host, RTP_VOICE)]

        self.voice_backend = "ffmpeg"
        self.voice_capture = (mic_name, pi_host, mic_gain)
        self.leg_env = {}
        if IS_MAC and mic_name and _gst():
            try:
                from audio_engine import mac_microphone_argv, mac_microphone_device_id
                a = mac_microphone_argv(_gst(), mic_name, pi_host, RTP_VOICE, mic_gain)
                self.voice_device_id = mac_microphone_device_id(mic_name) if mic_name == "System default microphone" else int(a[3].split("=", 1)[1])
                self.leg_env["voice"] = _gst_env()
                self.voice_backend = "gstreamer-coreaudio"
            except RuntimeError as exc:
                # If macOS has no input at all, keep video running until one exists.
                a = []
                self.voice_backend = "gstreamer-coreaudio"
                self.voice_device_id = None
                self.leg_env["voice"] = _gst_env()
                print("[mic] Waiting for an available microphone: %s" % exc)
            except ImportError as exc:
                print("[mic] CoreAudio capture unavailable; using legacy FFmpeg: %s" % exc)

        # Keep each leg's stderr so a dead leg can be explained instead of guessed at.
        self.logs = []
        self.voice_proc = None
        # Remember each leg's argv so ONE leg can be respawned without disturbing the others.
        # Previously a dead video leg meant restarting the whole session — a visible freeze
        # plus an audible gap — to fix a fault that only touched video.
        # The session is now intended to run. Everything that repairs a leg checks this, so
        # it must be set where a session BEGINS, not where a process happens to start.
        self.wanted = True
        self.leg_argv = {"video": v, "voice": a}
        self.leg_proc = {}
        logdir = _logdir()
        for name, argv in (("video", v), ("voice", a)):
            if name == "voice" and not argv:
                self.voice_proc = WaitingMicrophone()
                self.leg_proc[name] = self.voice_proc
                continue
            lf = open(os.path.join(str(logdir), "netbridge-source-%s.log" % name), "w")
            self.logs.append(lf)
            # stdin is a PIPE so this leg can be asked to quit POLITELY later. ffmpeg exits
            # cleanly on "q" and releases its capture device; killed with a signal while it
            # holds avfoundation it can leave the macOS camera daemons wedged - handing out
            # the device afterwards but never delivering frames. That happened on 2026-08-14
            # and cost a live session. See _quit().
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=lf,
                                    env=self.leg_env.get(name))
            self.procs.append(proc)
            self.leg_proc[name] = proc
            if name == "voice":
                self.voice_proc = proc          # so the app can report the voice leg is alive

        self.return_player = self._start_return(return_port) if self.return_on else "off"

    @_locked
    def set_voice_muted(self, muted):
        """Mute capture deliberately; supervisors must never undo the user's intent."""
        if not self.wanted:
            raise RuntimeError("Start a session before changing the microphone")
        self.voice_muted = bool(muted)
        if self.voice_muted:
            proc = getattr(self, "leg_proc", {}).get("voice")
            if proc is not None:
                _quit(proc)
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
                if proc.poll() is None:
                    self.voice_muted = False
                    raise RuntimeError("Microphone did not stop; end the session to retry")
        elif not self.voice_sending() and not self.respawn_leg("voice"):
            # Keep the privacy state truthful if capture cannot be restarted.
            self.voice_muted = True
            raise RuntimeError("Microphone could not restart; check your input device")
        return self.voice_muted

    @_locked
    def suspend_voice(self):
        """Stop a disconnected CoreAudio input without touching video/return."""
        if not self.wanted or getattr(self, "voice_backend", None) != "gstreamer-coreaudio":
            return
        proc = self.leg_proc.get("voice")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    @_locked
    def respawn_leg(self, name):
        """Restart ONE media leg in place. Returns True if it came back.

        The whole point is that the other legs never notice. Video dying is not a reason to
        interrupt the audio the room is listening to, and vice versa — a supervisor that
        rebuilds the session to fix one leg causes a bigger outage than the fault it is
        repairing. That mistake was made once tonight and it ended a live call."""
        # Refuse to resurrect a session the operator ended. Without this the supervisor
        # silently undoes a deliberate stop, which is how the camera stayed on.
        if not getattr(self, "wanted", False):
            return False
        if name == "voice" and getattr(self, "voice_muted", False):
            return False
        argv = (getattr(self, "leg_argv", {}) or {}).get(name)
        if not argv and not (name == "voice" and getattr(self, "voice_backend", None) == "gstreamer-coreaudio"):
            return False
        selected = getattr(self, "capture_names", {}).get(name)
        if selected and (name == "video" or IS_WIN):
            devices = av_devices(timeout=3).get("video" if name == "video" else "audio", [])
            matches = [d for d in devices if d.get("name") == selected]
            if not matches:
                self.waiting_for_devices[name] = selected
                return False
            self.waiting_for_devices.pop(name, None)
            argv = list(argv)
            if "-i" in argv:
                argv[argv.index("-i")+1] = (("video=" if name == "video" else "audio=") + selected
                    if IS_WIN else str(matches[0]["index"]) + ":none")
            self.leg_argv[name] = argv
        if name == "video":
            argv = bind_video_ticket(argv, PINS.ticket())
            self.leg_argv[name] = argv
        old = (getattr(self, "leg_proc", {}) or {}).get(name)
        if old is not None:
            # Same care as a full stop. A respawn happens exactly when something is already
            # wrong, which is the worst moment to SIGKILL a process holding the camera and
            # turn a recoverable fault into an unrecoverable one.
            _quit(old, timeout=3.0)
            if old in self.procs:
                self.procs.remove(old)
        try:
            if name == "voice" and getattr(self, "voice_backend", None) == "gstreamer-coreaudio":
                # USB reattachment can change the CoreAudio unique ID. Resolve
                # the selected device again instead of retrying a stale argv.
                from audio_engine import mac_microphone_argv, mac_microphone_device_id
                mic_name, host, gain = self.voice_capture
                argv = mac_microphone_argv(_gst(), mic_name, host, RTP_VOICE, gain)
                self.voice_device_id = mac_microphone_device_id(mic_name) if mic_name == "System default microphone" else int(argv[3].split("=", 1)[1])
                self.leg_argv[name] = argv
            lf = open(os.path.join(str(_logdir()), "netbridge-source-%s.log" % name), "w")
            self.logs.append(lf)
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=lf,
                                    env=getattr(self, "leg_env", {}).get(name))
        except Exception:
            return False
        self.procs.append(proc)
        self.leg_proc[name] = proc
        if name == "voice":
            self.voice_proc = proc
        return proc.poll() is None

    @_locked
    def adapt_video(self, check, now):
        if not self.wanted or not isinstance(check, dict) or check.get("source_state") != "live":
            return None
        choice = self.video_quality.observe(check.get("fps"), now)
        if choice is None:
            return None
        argv = list(self.leg_argv.get("video") or [])
        if not argv:
            return None
        fps, bitrate = choice
        for flag, value in (("-r", str(fps)), ("-g", str(fps)), ("-b:v", bitrate)):
            if flag not in argv:
                return None
            argv[argv.index(flag)+1] = value
        self.leg_argv["video"] = argv
        ok = self.respawn_leg("video")
        return "Video set to %d fps / %s%s" % (fps, bitrate, "" if ok else " — camera unavailable")

    def leg_cpu_rate(self, name):
        """CPU seconds per wall second this leg is currently burning, or None.

        A PROCESS BEING ALIVE IS NOT PROOF THAT A CAMERA IS PRODUCING FRAMES.
        ---------------------------------------------------------------------
        That distinction is the whole point. When the macOS capture daemons are left wedged -
        which happens when something is killed while holding the camera - avfoundation hands
        out the device and then delivers nothing. ffmpeg sits there perfectly healthy,
        `poll()` returns None, and the supervisor concludes the local end is fine and blames
        the far end. On 2026-08-24 the app told an operator to check the bridge while the
        fault was on the Mac the message was printed from.

        The tell is work done, not liveness. Measured on this hardware: a healthy 20fps
        encoder burned 26.81 CPU seconds over 81 wall seconds (~0.33), a wedged one managed
        0.23 over 56 (~0.004) - about eighty times less. The threshold is not those numbers:
        they are one machine on one day, and a faster Mac or a lighter stream would move them.
        What is robust is the ORDER of magnitude between working and not, so the caller
        compares against a floor far below any plausible real workload.
        """
        p = (getattr(self, "leg_proc", {}) or {}).get(name)
        if not p or p.poll() is not None:
            return None
        try:
            t = os.times()      # wall clock reference that does not depend on the system clock
            now = time.monotonic()
            r = subprocess.run(["ps", "-o", "time=", "-p", str(p.pid)],
                               capture_output=True, text=True, timeout=3)
            raw = (r.stdout or "").strip()
            if not raw:
                return None
            # ps prints [dd-]hh:mm:ss or mm:ss.ss depending on magnitude
            parts = raw.replace("-", ":").split(":")
            secs = 0.0
            for x in parts:
                secs = secs * 60 + float(x)
        except Exception:
            return None
        prev = (getattr(self, "_leg_cpu", {}) or {}).get(name)
        if not hasattr(self, "_leg_cpu"):
            self._leg_cpu = {}
        self._leg_cpu[name] = (secs, now)
        if not prev:
            return None                     # first sample: a rate needs two
        psecs, pnow = prev
        gap = now - pnow
        if gap < 2.0 or gap > 300:          # too short to be meaningful, or a stale sample
            return None
        return max(0.0, (secs - psecs) / gap)

    # Far below any real encode - a 320x180 stream at 20fps sits two orders of magnitude
    # above this - and far above an idle process that merely wakes up occasionally.
    CPU_FLOOR = 0.02

    def leg_status(self):
        """Which media legs are alive right now, by name."""
        out = {}
        for name, p in (getattr(self, "leg_proc", {}) or {}).items():
            out[name] = bool(p and p.poll() is None)
        if self.voice_muted:
            out["voice"] = None
        out["return"] = bool(self.return_proc and self.return_proc.poll() is None) \
            if self.return_on else None      # None = deliberately off, not a fault
        return out

    def audio_settings(self):
        return {"gain": float(self.return_gain), "jitter_ms": int(self.return_jitter_ms),
                "dynamics": self.return_dynamics, "sink_sync": self.return_sink_sync,
                "fec": self.return_fec, "plc": self.return_plc,
                "clock_mode": self.return_clock_mode}

    def audio_snapshot(self):
        player = self.return_proc
        if player and hasattr(player, "snapshot"):
            return player.snapshot()
        return {"backend": self.return_player, "running": bool(player and player.poll() is None),
                "detail": getattr(self, "audio_backend_note", "Start return playback to view diagnostics")}

    @_locked
    def set_return_tuning(self, gain=None, jitter_ms=None, dynamics=None, sink_sync=None, conceal=None,
                          fec=None, plc=None, clock_mode=None):
        """Change return-audio gain / buffer depth WITHOUT ending the session.

        These were env-vars read once at app launch, so trying a different value meant
        quitting mid-meeting - which is exactly when you discover you need one. The two
        documented failure modes both live here: loud media "pumping" through the
        compressor (lower the gain) and Wi-Fi timing bursts (raise the buffer). A presenter
        should be able to fix what they are hearing while they are hearing it."""
        def settings():
            # Canonicalize numeric strings: a repeated 1.0 -> "1.00" update is
            # not a reason to interrupt audio. Invalid launch overrides remain
            # comparable so a valid live update can still repair them.
            def number(value):
                try: return float(value)
                except (TypeError, ValueError): return value
            return (number(self.return_gain), number(self.return_jitter_ms),
                    self.return_dynamics, self.return_sink_sync,
                    self.return_fec, self.return_plc, self.return_clock_mode)
        before = settings()
        if clock_mode in ("slave", "none"):
            self.return_clock_mode = clock_mode
        if gain is not None:
            try: self.return_gain = "%.2f" % max(0.2, min(4.0, float(gain)))
            except (TypeError, ValueError): pass
        if jitter_ms is not None:
            try: self.return_jitter_ms = str(int(max(0, min(1000, int(jitter_ms)))))
            except (TypeError, ValueError): pass
        if dynamics is not None:
            self.return_dynamics = bool(dynamics)
        if sink_sync is not None:
            self.return_sink_sync = bool(sink_sync)
        if conceal is not None:
            self.return_conceal = bool(conceal)
            # Keep the old single switch working: turning conceal ON means "do everything",
            # which is what any existing caller meant by it.
            self.return_fec = bool(conceal)
            self.return_plc = bool(conceal)
        # Explicit knobs win over the legacy switch, so the two can be tested apart. This is
        # the whole point of splitting them: FEC repairs real loss, PLC invents audio, and
        # they must be answerable separately.
        if fec is not None:
            self.return_fec = bool(fec)
        if plc is not None:
            self.return_plc = bool(plc)
        if self.return_on and settings() != before:
            if self.return_proc and hasattr(self.return_proc, "tune") and self.return_proc.poll() is None:
                self.return_proc.tune(self.audio_settings())
            else:
                # Legacy runtime cannot change element properties in place.
                self.set_return(False)
                self.set_return(True)
        return {"gain": self.return_gain, "jitter_ms": self.return_jitter_ms,
                "dynamics": self.return_dynamics, "sink_sync": self.return_sink_sync,
                "conceal": self.return_conceal, "fec": self.return_fec,
                "plc": self.return_plc, "player": self.return_player,
                "clock_mode": self.return_clock_mode}

    @_locked
    def set_return(self, on):
        """Toggle 'Play meeting audio here' live, without disturbing the video/voice legs.
        Off stops just the local return player (the bridge keeps sending; you simply don't
        play it here — e.g. when you're listening on the meeting device itself)."""
        self.return_on = bool(on)
        running = self.return_proc and self.return_proc.poll() is None
        # The return player is counted in self.procs, so starting one would make `live` true
        # again on a session the operator has ended - and the supervisor calls this to repair
        # a dead return leg. Remember the preference either way; just do not act on it when
        # there is no session to play into.
        if self.return_on and not running and not getattr(self, "wanted", False):
            # False, not a dict: this function normally returns a bool and the supervisor
            # treats the result as "did the repair work". A dict is truthy, so returning one
            # here would report a success that did not happen.
            return False
        if self.return_on and not running:
            self.return_player = self._start_return(self.return_port)
        elif not self.return_on and running:
            try:
                self.return_proc.terminate()
            except Exception:
                pass
            if self.return_proc in self.procs:
                self.procs.remove(self.return_proc)
            self.return_proc = None
            self.return_player = "off"
        return self.return_on

    def _start_return(self, port):
        """Play the meeting room's audio back. Returns a label for what is playing it.

        GStreamer first: rtpjitterbuffer holds ~250ms and releases at a steady rate, which
        is what makes WiFi-jittered return audio listenable. ffmpeg is the fallback so a
        presenter who only has the bundled binary still HEARS the room instead of silence
        - it has no equivalent jitter buffer, so expect it to be rougher.

        Returning a label matters: previously a missing GStreamer meant this block was
        skipped entirely, with no error and no sound. Silence that looks like success is
        the worst outcome here, because the presenter cannot tell it from a quiet room.
        """
        if os.environ.get("NB_AUDIO_ENGINE", "persistent") != "legacy":
            try:
                from audio_engine import AudioEngine
                engine = AudioEngine(port, self.audio_settings(), _logdir())
                self.procs.append(engine)
                self.return_proc = engine
                return "gstreamer-persistent"
            except ImportError as exc:
                self.audio_backend_note = "Persistent receiver unavailable: %s; using legacy player" % exc
            except Exception as exc:
                self.audio_backend_note = "Persistent receiver failed: %s" % exc
                print("[audio] %s" % self.audio_backend_note, flush=True)
                return "none"
        else:
            self.audio_backend_note = "Legacy receiver explicitly selected; live telemetry unavailable"
        caps = ("application/x-rtp,media=audio,encoding-name=OPUS,"
                "payload=97,clock-rate=48000")
        gst = _gst()
        if gst:
            try:
                # Return RTP reaches localhost through the embedded mesh helper.
                # Keep timing, gain and dynamics independently tunable so listening
                # comparisons do not conflate clock correction with processing artifacts.
                lat = getattr(self, "return_jitter_ms", None) or os.environ.get("NB_RETURN_JITTER_MS", "250")
                gain = getattr(self, "return_gain", None) or os.environ.get("NB_RETURN_GAIN", "1.0" if IS_MAC else "2.0")
                # LOSS AND LATENESS ARE DIFFERENT FAULTS, AND ONE FLAG WAS DECIDING BOTH.
                #
                # `return_conceal` switched three things at once: do-lost, use-inband-fec and
                # plc. It was turned OFF because PLC's guesses were audible as warble - correct,
                # and it stays off. But that also disabled inband-FEC, which is not a guess:
                # the bridge encodes with `inband-fec=true packet-loss-percentage=20`, so every
                # packet already carries a redundant copy of the previous frame. The decoder was
                # discarding it. The bridge has been spending a fifth of its bitrate on
                # redundancy that this machine threw away.
                #
                # That mattered once the fault was measured instead of assumed. On 2026-08-24
                # the link showed 6.7-10% PACKET LOSS at 6-15ms latency - things going missing,
                # not arriving late. A deeper jitter buffer cannot repair a packet that never
                # came, which is why every buffer change this week did nothing.
                #
                # FEC needs do-lost: the decoder only reaches for the redundant copy when the
                # jitterbuffer tells it a packet is missing. So FEC = do-lost + use-inband-fec,
                # and PLC stays a separate switch that defaults OFF. Where there is no redundant
                # copy, opusdec emits silence rather than inventing something.
                fec = getattr(self, "return_fec", True)
                plc = getattr(self, "return_plc", False)
                chain = [gst, "-q",
                    "udpsrc", "port=%d" % port, "caps=" + caps, "!",
                    "rtpjitterbuffer", "latency=" + lat] + (
                        ["do-lost=true"] if (fec or plc) else []) + ["!",
                    "rtpopusdepay", "!",
                    "opusdec"] + (["use-inband-fec=true"] if fec else []) \
                               + (["plc=true"] if plc else []) + ["!",
                    "audioconvert", "!", "audioresample", "quality=10", "!",
                ]
                if getattr(self, "return_dynamics", not IS_MAC):
                    chain += [
                        "audiodynamic", "mode=compressor", "characteristics=soft-knee",
                            "ratio=0.1", "threshold=0.12", "!",
                        "volume", "volume=" + gain, "!",
                        "audiodynamic", "mode=compressor", "characteristics=hard-knee",
                            "ratio=0.08", "threshold=0.97", "!"]
                else:
                    chain += ["volume", "volume=" + gain, "!"]   # gain only, no dynamics
                chain += [
                    "audioconvert", "!",
                    "queue", "max-size-time=400000000", "!"]
                sync = "true" if getattr(self, "return_sink_sync", IS_MAC) else "false"
                if IS_MAC:
                    chain += ["osxaudiosink", "sync=" + sync,
                              "buffer-time=200000", "latency-time=20000"]
                else:
                    chain += ["autoaudiosink", "sync=" + sync]
                # Preserve warnings/errors instead of discarding the evidence needed
                # to distinguish sink failures from damaged incoming audio. Keep the
                # previous run across a tuning restart, and close the parent's handle.
                log_path = _logdir() / "netbridge-source-return.log"
                if log_path.exists():
                    try:
                        log_path.replace(log_path.with_suffix(".log.previous"))
                    except OSError:
                        # Windows may still have the terminated child's file open.
                        # A failed archive must not prevent return playback restarting.
                        pass
                env = _gst_env()
                env.setdefault("GST_DEBUG", "2")
                env.setdefault("GST_DEBUG_NO_COLOR", "1")
                with log_path.open("w") as lf:
                    lf.write("Return playback: gain=%s dynamics=%s sync=%s jitter_ms=%s fec=%s plc=%s\n" %
                             (gain, getattr(self, "return_dynamics", not IS_MAC), sync, lat, fec, plc))
                    lf.flush()
                    p = subprocess.Popen(chain, stdout=subprocess.DEVNULL,
                                         stderr=lf, env=env)
                self.procs.append(p)
                self.return_proc = p
                return "gstreamer"
            except Exception:
                pass

        # ffmpeg fallback — but ONLY where ffmpeg can actually play audio out. It has an
        # output device on macOS (audiotoolbox) and Linux (alsa); on WINDOWS it has NONE
        # (-f sdl is a VIDEO display, not audio), so a fallback there would open a blank
        # window and play silence. On Windows, GStreamer is the only option, so if the
        # bundled GStreamer is missing we say so plainly instead of faking a player.
        if IS_WIN:
            return "none"
        sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=NetBridge return\r\n"
               "c=IN IP4 0.0.0.0\r\nt=0 0\r\nm=audio %d RTP/AVP 97\r\n"
               "a=rtpmap:97 opus/48000/2\r\n" % port)
        sdp_path = os.path.join(str(_logdir()), "return.sdp")
        try:
            with open(sdp_path, "w") as f:
                f.write(sdp)
        except Exception:
            return "none"
        out = ["-f", "audiotoolbox", "-"] if IS_MAC else ["-f", "alsa", "default"]
        try:
            lf = open(os.path.join(str(_logdir()), "netbridge-source-return.log"), "w")
            self.logs.append(lf)
            p = subprocess.Popen(
                [_ffmpeg(), "-hide_banner", "-loglevel", "warning",
                 "-protocol_whitelist", "file,udp,rtp", "-i", sdp_path] + out,
                stdout=subprocess.DEVNULL, stderr=lf)
            self.procs.append(p)
            self.return_proc = p
            return "ffmpeg"
        except Exception:
            return "none"

    @_locked
    def stop(self):
        # Record the intent FIRST. If this were set after the kills, a supervisor tick landing
        # in between would see dead legs, believe they crashed, and restart them.
        self.wanted = False
        self.voice_muted = False
        # Ask every leg to quit and release its device, in parallel, before escalating. The
        # old version went terminate -> kill on a 4s budget shared across ALL processes, so
        # the last leg in the list could be SIGKILLed almost immediately - and a SIGKILLed
        # ffmpeg is what leaves the camera wedged. _quit() gives each one its own escalation.
        for p in self.procs:
            try:
                if p.stdin and not p.stdin.closed:
                    p.stdin.write(b"q\n"); p.stdin.flush()
            except Exception:
                pass
        for p in self.procs:
            _quit(p)
        self.procs = []
        self.return_proc = None



def _quit(p, timeout=4.0):
    """Stop one media process without wedging the hardware it holds.

    WHY THE ORDER MATTERS
    ---------------------
    ffmpeg reading from avfoundation must be allowed to close the device itself. Ask first
    ("q" on stdin, its documented graceful quit), then SIGTERM, and only SIGKILL as a last
    resort. Going straight to a signal - and especially to SIGKILL - can leave the macOS
    capture daemons handing out the camera while delivering no frames at all, which looks
    exactly like a broken bridge and is not fixable from inside this app.

    That is not hypothetical: on 2026-08-14 the app was killed mid-capture, every later
    launch got a camera that opened and produced nothing, the supervisor restarted the video
    leg three times against a fault no restart could reach, and the session was lost.

    Each escalation is skipped if the process is already gone, so the normal path costs
    nothing.
    """
    if p is None:
        return
    if hasattr(p, "snapshot") and hasattr(p, "tune"):
        p.terminate()
        return
    try:
        if p.poll() is not None:
            return
    except Exception:
        return
    try:
        if p.stdin and not p.stdin.closed:
            p.stdin.write(b"q\n")
            p.stdin.flush()
    except Exception:
        pass
    for step in ("asked", "term"):
        try:
            p.wait(timeout=timeout if step == "asked" else 2.0)
            return
        except Exception:
            pass
        if step == "asked":
            try: p.terminate()
            except Exception: pass
    try:
        p.kill()
    except Exception:
        pass


_SUSPENDING = threading.Event()
_POWER_OBSERVER = None


def suspend_capture():
    # Native sleep callback: privacy intent is set before any process cleanup.
    _SUSPENDING.set()
    if not SESSION.wanted:
        return
    SESSION.wanted = False
    SESSION.stop()
    SESSION.interruption = "Session paused for sleep. Choose Go live to resume."
    ticket, port = PINS.ticket(), getattr(MESH, "control_port", None)
    PINS.clear()
    # Capture the old ticket NOW: delayed cleanup after wake must never end a new session.
    def retire():
        if ticket and port:
            api("POST", "http://127.0.0.1:%d/api/end-session" % port,
                body={"ticket": ticket}, timeout=2)
    threading.Thread(target=retire, daemon=True).start()


def resume_capture():
    _SUSPENDING.clear()  # Explicit Go live is the only path that starts capture again.


SESSION = Session()


MESH_PROC = "netbridge-mesh.exe" if IS_WIN else "netbridge-mesh"
MEDIA_PROCS = ("ffmpeg", "gst-launch-1.0")
MEDIA_MARKS = ("dump_extra=freq=keyframe", "payload_type 97", "udpsrc port=5004", "name=netbridge_presenter_microphone")


def _proc_name(pid):
    """The executable's own name for `pid` - never anything from its arguments - or '' if gone."""
    try:
        out = subprocess.run(["ps", "-o", "ucomm=" if IS_MAC else "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""
    return os.path.basename(out)


def _kill_orphan_media(names=MEDIA_PROCS, marks=MEDIA_MARKS):
    """Startup-only reaper for OUR leftover ffmpeg/GStreamer from a hard-killed prior run.
    Normal exits are handled by the signal cleanup in main(); a SIGKILL/crash can't run any
    handler, so a fresh launch sweeps up its own zombies here. Matched by unmistakable arg
    signatures unique to us (our RTP video flag, our voice payload, our return udpsrc port),
    so no unrelated ffmpeg/gst on the machine is ever touched - AND only in a process that
    really is ffmpeg/gst-launch: `pgrep -f` matches whole command lines, so a Terminal `grep`
    or an editor with one of these strings in its arguments used to be SIGKILLed too
    (2026-09-25). Safe only at startup, before this instance has a live session of its own."""
    if IS_WIN:
        return  # rely on the signal cleanup; a broad image kill would be unsafe on Windows
    import signal as _sig
    for pat in marks:
        try:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            for tok in out.stdout.split():
                try:
                    pid = int(tok)
                    if _proc_name(pid) not in names:
                        continue      # our signature in some OTHER program's arguments: not ours
                    os.kill(pid, _sig.SIGKILL)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except Exception:
            pass


def _bridge_reachable(host, st):
    """Is the bridge answering AT ALL, by any route? Used only to tell a presenter WHICH
    failure they have: a device that is off is a different problem from one that is up but
    whose mesh path has not come up yet, and 'timed out' hid that distinction completely.
    Tries the bridge's LAN address directly — deliberately NOT through the mesh helper,
    since the helper is the thing under suspicion."""
    try:
        rec = _bridge_rec(host, st) or {}
        seen = []
        for addr in filter(None, [rec.get("ip"), rec.get("tailscale_ip"), host]):
            if addr in seen:
                continue                      # the id often equals one of the addresses
            seen.append(addr)
            # Short timeout ON PURPOSE: this runs while the presenter waits for the bridge
            # dropdown. A reachable bridge answers on the first probe; a genuinely dead one
            # must not stall the UI for ten seconds to tell you what it already said.
            r = api("GET", "http://%s:8080/api/status" % addr, timeout=2)
            if not r.get("_error"):
                return True
    except Exception:
        pass
    return False


def _mesh_bin():
    """The embedded mesh client. PREFER a SIDECAR next to the app binary: PyInstaller
    strips/re-signs any Mach-O it bundles, which corrupts this Go binary and silently kills
    inbound UDP over tsnet (return audio dead). The sidecar is copied verbatim, so tsnet's
    UDP receive works. Falls back to the bundled/dev copy if no sidecar is present."""
    name = "netbridge-mesh.exe" if IS_WIN else "netbridge-mesh"
    cands = []
    if getattr(sys, "frozen", False):                 # packaged app: sidecar next to the exe
        cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name))
    base = _media_base()
    cands.append(os.path.join(base, name))            # bundled (processed — last resort)
    cands.append(os.path.join(base, "mesh", name))    # dev tree
    for cand in cands:
        if os.path.exists(cand):
            if not IS_WIN:
                try:
                    os.chmod(cand, 0o755)
                except Exception:
                    pass
            return cand
    return None


def _kill_orphan_mesh(exclude_pid=None, name=None):
    """Reap leftover netbridge-mesh helpers from a previous/crashed session.

    An orphaned helper keeps holding the media + control ports (5000/5002/5004/18080).
    The next go-live's helper then can't bind them and the session half-fails — the
    classic 'unlock timed out' with the video/voice checks stuck red. We own at most one
    helper (self.proc) which callers stop() first, so killing every other netbridge-mesh
    here is safe. Best-effort: never let cleanup raise into the go-live path.

    Matched by the PROCESS NAME, exactly (pgrep -x; taskkill /IM on Windows). It was
    `pgrep -f netbridge-mesh`, which matches whole command lines: at every launch and every
    go-live it SIGKILLed any process that merely MENTIONED the helper - a Terminal grep, an
    editor, a build (found 2026-09-25 when it killed a test shell)."""
    import signal as _sig
    name = name or MESH_PROC
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            out = subprocess.run(["pgrep", "-x", name],
                                 capture_output=True, text=True)
            for tok in out.stdout.split():
                try:
                    pid = int(tok)
                except ValueError:
                    continue
                if exclude_pid and pid == exclude_pid:
                    continue
                for s in (_sig.SIGTERM, _sig.SIGKILL):
                    try:
                        os.kill(pid, s)
                    except (ProcessLookupError, PermissionError):
                        break
    except Exception:
        pass


class MeshManager:
    """Runs the embedded mesh client so the app reaches the bridge over the private mesh
    (walkthrough J3: "joins the private mesh with an embedded client + scoped token from
    sign-in", "via secure mesh"). The presenter never sees a 100.x address: everything is
    routed through 127.0.0.1 proxies the helper owns.

    route() decides per bridge. If the bridge has a tailnet address and we have the helper
    and can mint a scoped key, it brings the mesh up and returns localhost proxy targets.
    A bridge with no tailnet address (pre-claim / bench) is reached directly - the same
    app, adapting, not a second code path bolted on.
    """
    CTRL_LOCAL = 18080

    def __init__(self):
        self.proc = None
        self.bridge_id = None
        self.tailnet_ip = None      # OUR mesh IP; the bridge returns audio here
        self.control_port = None
        self.key_error = None
        self.bridge_lan_ip = None   # the bridge's LAN IP, if it's on our network

    def _mint_key(self, st):
        self.key_error = None
        r = api("POST", st["control_url"].rstrip("/") + "/auth/mesh-key",
                token=st.get("token"), timeout=25)
        if isinstance(r, dict) and not r.get("_error"):
            # the endpoint returns the key as "authkey" (not "key")
            return r.get("authkey"), r.get("login_server") or ""
        self.key_error = _signin_error(r)
        return None, ""

    def route(self, rec, st):
        """rec: {id, tailscale_ip, ip}. Returns routing for control + media + return."""
        tsip = (rec or {}).get("tailscale_ip")
        mesh_bin = _mesh_bin()
        # Per the walkthrough (J3), a claimed bridge is reached OVER THE SECURE MESH — the
        # embedded helper joins the tailnet and proxies media/control. On the same LAN the
        # tailnet transparently uses the direct LAN path anyway (fast), so this is not slow;
        # it just keeps the presenter off any 100.x address. If the bridge has no tailnet
        # address, or we lack the helper/key, we fall through to a direct connection below.
        if not (tsip and mesh_bin and st.get("token") and st.get("control_url")):
            self.stop()
            # NO LAN FALLBACK. The walkthrough says the presenter reaches a claimed bridge
            # "via secure mesh" and never mentions a LAN path; Samith has ruled it out
            # explicitly. This used to silently downgrade to a direct LAN connection, which
            # is how a session once reported LIVE while media went nowhere — the fallback
            # had no usable address either, so it failed anyway, just quietly and with the
            # wrong explanation. Refusing out loud is strictly better than a forbidden path.
            missing = [n for n, v in (("bridge mesh address", tsip), ("mesh helper", mesh_bin),
                                      ("sign-in", st.get("token")),
                                      ("control plane URL", st.get("control_url"))) if not v]
            return {"via": "none", "error": "cannot reach the bridge over the mesh — missing: "
                                            + ", ".join(missing)}

        # reuse a live helper for the same bridge
        if self.proc and self.proc.poll() is None and self.bridge_id == rec.get("id"):
            return self._mesh_route()

        self.stop()
        # Reap any orphan helper from a prior/crashed session BEFORE launching, so the new
        # helper can bind its ports cleanly (orphans holding 5000/5002/5004/18080 were the
        # root of the recurring 'unlock timed out').
        _kill_orphan_mesh()
        key, login = self._mint_key(st)
        if not key:
            # Same rule: no silent LAN downgrade. A refused mesh key means the control plane
            # would not issue one (TS_API_KEY missing, tag policy, expiry) — a real fault
            # worth surfacing, not something to paper over with a path we do not support.
            return {"via": "none",
                    "error": self.key_error or "Fleet could not create a secure connection. Contact your fleet admin."}

        # The key goes in the environment, not argv: argv is readable by every process on
        # the machine (ps), and this key admits a node to the tailnet.
        argv = [mesh_bin, "--bridge", tsip,
                "--hostname", _mesh_hostname(rec, st),
                "--forward", "%d,%d" % (RTP_VIDEO, RTP_VOICE), "--return", "5004",
                "--control", "%d:8080" % self.CTRL_LOCAL]
        if login:
            argv += ["--login-server", login]
        lf = open(os.path.join(str(_logdir()), "netbridge-source-mesh.log"), "w")
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=lf, text=True,
                             env=dict(os.environ, NB_MESH_AUTHKEY=key))
        # read the one-line handshake (helper prints it only once the proxies are wired)
        line = ""
        try:
            for _ in range(60):
                line = p.stdout.readline()
                if line.strip():
                    break
        except Exception:
            pass
        try:
            hs = json.loads(line or "{}")
        except Exception:
            hs = {}
        if not hs.get("ready"):
            p.terminate()
            ip = rec.get("ip")
            return {"via": "none",
                    "error": "the mesh helper did not start: %s" % hs.get("error", "no handshake")}
        self.proc, self.bridge_id = p, rec.get("id")
        self.tailnet_ip = hs.get("tailnet_ip")
        self.bridge_lan_ip = (rec or {}).get("ip")
        self.control_port = int(hs.get("control_port") or self.CTRL_LOCAL)
        return self._mesh_route()

    def _mesh_route(self):
        # EVERYTHING rides the secure mesh — control, forward media, AND return audio — per
        # the walkthrough (J3: "Bridge online · via secure mesh"). The bridge sends the return
        # stream to OUR tailnet IP; the embedded helper's returnLeg receives it over the mesh
        # and hands it to the local player. This requires the tailnet ACL grant
        # `bridge -> tag:source udp:5004`, which is configured. No LAN shortcut — the HTML
        # never mentions one, and Samith explicitly banned it.
        return {"via": "mesh", "control_host": "127.0.0.1", "control_port": self.control_port,
                "media_host": "127.0.0.1", "return_peer": self.tailnet_ip}

    def health(self):
        """Is the helper actually alive, and does the app know why if not?

        THE FAILURE THIS EXISTS FOR. The helper carries the meeting's audio BACK to the
        presenter. When it dies, everything the presenter can see keeps working: the app is up,
        the camera light is on, the room sees and hears them. Only the return path is gone, and
        the presenter cannot tell whether the room is silent or whether they have been cut off.

        Until 26 Aug 2026 nothing reported this at all. `MESH.proc` appeared in exactly one
        place in the whole app -- the leg watchdog -- and there it SILENCED the alarm: a dead
        helper took the same branch as "no session running", cleared the missing-legs list, and
        the UI reported legs ok. A dead helper made the app look healthier, not less healthy.
        """
        # MeshManager has no intent flag of its own; the helper exists only for the duration
        # of a session, so SESSION.wanted is the right question. Using `self.proc is None`
        # alone would report "missing" for the entirely normal idle case.
        if not getattr(SESSION, "wanted", False):
            return {"state": "not_started", "ok": True,
                    "detail": "no session running; the helper only runs while live"}
        if not self.proc:
            return {"state": "missing", "ok": False,
                    "detail": "the mesh helper was never started. Return audio cannot work: "
                              "the room will see and hear you, and you will hear nothing back.",
                    "fix": "Quit and relaunch the app. If it persists, check that "
                           "netbridge-mesh sits beside the app and is not quarantined "
                           "(xattr -dr com.apple.quarantine .)"}
        rc = self.proc.poll()
        if rc is None:
            return {"state": "running", "ok": True, "pid": self.proc.pid}
        return {"state": "dead", "ok": False, "exit_code": rc,
                "detail": "the mesh helper EXITED (code %s). The room can still see and hear "
                          "you; you will hear nothing back, and nothing else will look wrong."
                          % rc,
                "fix": "Stop and go live again. If it dies immediately, the helper is most "
                       "likely quarantined or missing next to the app."}

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=4)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = self.bridge_id = self.tailnet_ip = self.control_port = None
        self.bridge_lan_ip = None


MESH = MeshManager()
_BRIDGES = {"list": [], "ts": 0.0}


# ---------------------------------------------------------------- the bridge PIN (2026-09-25)
#
# The owner went live without typing a PIN. Go live never asked for one, and the bridge took
# video from anyone on the mesh. Bridges from the 2026-09-25 image require a PIN session for
# every go-live: a correct PIN returns a TICKET, set-peer needs it, Stop ends it, and the
# bridge's media gate admits video and voice only from the presenter who holds the session.
#
# The ticket lives HERE, in this process's memory, and nowhere else: never on disk, never in
# state.json, never sent to the page. It is dropped on Stop, on sign-out, when the bridge says
# it is no longer valid, and when the app exits. An internal reconnect (a media leg dropped, the
# mesh helper was rebuilt) keeps it: that is the same session, not a new go-live.
PIN_PROTOCOL = 2

PIN_MESSAGES = {
    "wrong": "Wrong PIN — {left} more {tries} before the bridge locks for an hour.",
    "locked_out_now": "Wrong PIN. That was the third wrong try, so the bridge is now locked for "
                      "1 hour. Your admin can clear the lockout from the fleet.",
    "locked_out": "This bridge is locked after 3 wrong PINs. Try again in {mins} min, or ask your "
                  "admin to clear the lockout from the fleet.",
    "no_pin": "This bridge has no PIN yet, so nobody can go live on it. Ask your admin to set one "
              "in the fleet (the bridge → Actions → Set PIN).",
    "bad_format": "The PIN is 4 to 8 digits.",
    "old_bridge": "This bridge runs older software that cannot check a PIN, so NetBridge will not "
                  "go live on it. Ask your admin for a compatible, verified bridge and presenter release.",
}
# Why the bridge refused a ticket (its 401 on set-peer) or reported itself locked mid-session.
SESSION_LOST = {
    "no_session": "The bridge is locked. Enter the PIN to go live.",
    "locked": "The bridge locked itself. Enter the PIN to continue.",
    "stop": "The session was ended on the bridge. Enter the PIN to go live again.",
    "idle": "The bridge locked itself after 10 minutes without video. Enter the PIN to continue.",
    "max_age": "The session reached its 12-hour limit. Enter the PIN to continue.",
    "expired": "The session expired on the bridge. Enter the PIN to continue.",
    "admin": "An admin locked this bridge. Enter the PIN to go live again.",
    "superseded": "Someone else entered the PIN on this bridge, so your session ended.",
    "pin_changed": "The bridge's PIN was changed. Enter the new PIN to continue.",
    "invalid": "Your session is no longer valid on the bridge. Enter the PIN again.",
    "reboot": "The bridge restarted, so it locked itself. Enter the PIN to reconnect.",
}


class PinSession:
    """The go-live ticket for ONE bridge, in memory only. Never logged, never persisted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._host = self._ticket = None
        self._since = 0.0
        self.lost = None        # why the bridge ended our session, for the page

    def set(self, host, ticket):
        with self._lock:
            self._host, self._ticket, self._since, self.lost = host, ticket, time.time(), None

    def ticket(self, host=None):
        with self._lock:
            if self._ticket and (host is None or host == self._host):
                return self._ticket
            return None

    def host(self):
        with self._lock:
            return self._host

    def clear(self, lost=None):
        with self._lock:
            self._host = self._ticket = None
            self._since = 0.0
            self.lost = lost

    def snapshot(self):
        with self._lock:
            return {"unlocked": bool(self._ticket), "host": self._host,
                    "since": int(self._since) or None, "lost": self.lost,
                    "lost_message": SESSION_LOST.get(self.lost) if self.lost else None}


PINS = PinSession()


def _pin_message(reason, j=None):
    j = j or {}
    left = j.get("attempts_left")
    return PIN_MESSAGES.get(reason, j.get("message") or "The bridge refused the PIN.").format(
        left=left if left is not None else "a few", tries="try" if left == 1 else "tries",
        mins=max(1, int(j.get("retry_in") or 3600) // 60))


def _bridge_pin_protocol(route):
    """(protocol, lock-state) - 2 = tickets (this image), 1 = the old gate, None = no answer.

    Asked BEFORE unlocking, every time: on an old bridge /api/unlock restarted ALL media, the
    operation that rebooted under-powered bridges, and it enforced nothing anyway."""
    r = api("GET", route["base"] + "/api/lock-state", timeout=8)
    if not isinstance(r, dict) or r.get("_error"):
        return None, r
    try:
        return int(r.get("protocol") or 1), r
    except (TypeError, ValueError):
        return 1, r


def _unlock_bridge(host, st, pin):
    """Verify the PIN ON THE BRIDGE and keep its ticket. -> dict for the page (never the ticket).

    Read-only protocol probes may retry while the mesh comes up. Once a PIN POST
    is sent, a lost response is ambiguous: the bridge may already have counted it.
    Never resend a PIN automatically or rebuild the helper after that POST."""
    if not re.fullmatch(r"[0-9]{4,8}", pin or ""):
        return {"ok": False, "reason": "bad_format", "need_pin": True,
                "message": PIN_MESSAGES["bad_format"]}
    last = None
    for attempt in range(3):
        route = bridge_route(host, st)
        if route.get("via") == "none":
            return {"ok": False, "reason": "no_route",
                    "message": route.get("error", "no route to the bridge")}
        proto, last = _bridge_pin_protocol(route)
        if proto is not None:
            if proto < PIN_PROTOCOL:
                return {"ok": False, "reason": "old_bridge", "message": PIN_MESSAGES["old_bridge"]}
            # protocol 2: this app keeps the ticket in memory. Older apps get no ticket back.
            last = api("POST", route["base"] + "/api/unlock", body={"pin": pin, "protocol": PIN_PROTOCOL, "video_binding": "ssrc-sha256-31-v1"}, timeout=15)
            if not isinstance(last,dict) or last.get("_error"):
                return {"ok": False, "reason": "unconfirmed", "attempts": 1,
                        "message": "The PIN result could not be confirmed. It was not retried automatically. Check the bridge connection before trying again."}
            break
        if attempt == 0:
            MESH.stop()                             # rebuild a helper that cannot route
            _kill_orphan_mesh()
        time.sleep(2)
    else:
        reachable = _bridge_reachable(host, st)
        return {"ok": False, "reason": "unreachable", "attempts": 3,
                "message": ("the bridge is not answering — check it has power and is online"
                            if not reachable else
                            "the bridge is up but not reachable over the mesh yet; it may still "
                            "be starting up — wait a few seconds and try again")}
    if last.get("ok") and last.get("ticket"):
        PINS.set(host, last["ticket"])
        if getattr(SESSION, "wanted", False):
            # A renewed PIN creates a new epoch. Rebind video only; healthy audio stays up.
            SESSION.respawn_leg("video")
        return {"ok": True, "reason": "ok", "unlocked": True,
                "message": "unlocked — this session ends when you press End session",
                "idle_timeout": last.get("idle_timeout"), "expires_in": last.get("expires_in")}
    reason = last.get("reason") or "error"
    return {"ok": False, "reason": reason, "need_pin": reason in ("wrong", "bad_format"),
            "attempts_left": last.get("attempts_left"), "retry_in": last.get("retry_in"),
            "message": _pin_message(reason, last)}


def _end_bridge_session():
    """Stop: end the bridge's PIN session while the mesh helper can still reach it. Best effort -
    if it cannot be reached, the bridge relocks by itself after 10 min without video."""
    ticket = PINS.ticket()
    port = getattr(MESH, "control_port", None)
    ended = False
    if ticket and port and MESH.proc and MESH.proc.poll() is None:
        r = api("POST", "http://127.0.0.1:%d/api/end-session" % port, body={"ticket": ticket}, timeout=5)
        ended = bool(isinstance(r, dict) and r.get("ended"))
    PINS.clear()
    return ended


def _fleet_bridges(st):
    """The org's bridges for the dropdown. /auth/bridges (fleet from 2026-09-25) answers any
    signed-in user; older fleets only have /admin/devices, which the new fleet reserves for
    admins - so try the new one first and fall back only when it does not exist."""
    base = st["control_url"].rstrip("/")
    r = api("GET", base + "/auth/bridges", token=st.get("token"))
    if isinstance(r, dict) and r.get("_code") in (404, 405):
        r = api("GET", base + "/admin/devices", token=st.get("token"))
        if isinstance(r, list):
            r = [{"id": d.get("id"), "number": d.get("number"), "label": d.get("label"),
                  "name": d.get("name") or d.get("pairing_code"), "pairing_code": d.get("pairing_code"),
                  "online": d.get("online"), "tailscale_ip": d.get("tailscale_ip"),
                  "ip": (d.get("latest") or {}).get("ip")}
                 for d in r if d.get("claimed", True)]
    return r


# ---------------------------------------------------------------- media-leg watchdog
#
# The helper is asked for three legs (--forward 5000,5002 --return 5004) and answers a
# one-line handshake. Until now the app trusted that answer for the rest of the session and
# never looked again. On 7 Aug a live session lost BOTH forward legs while the helper process
# stayed alive: ffmpeg kept encoding at 20 fps and firing packets at 127.0.0.1:5000, nothing
# was listening, and the bridge reported `feeder ... used 0 cpu ticks`. The app showed LIVE
# throughout and blamed the presenter's camera.
#
# The return leg survives these events independently, which is what makes them so confusing:
# the room still comes through perfectly, so it does not feel like a connection problem.
#
# So: keep asking. A leg that is gone is a fact the app can check in microseconds, and
# "silence that looks like success" is the failure mode this whole product keeps re-learning.

def _leg_bound(port, host="127.0.0.1"):
    """True if something already holds this UDP port.

    We probe by attempting the bind ourselves rather than parsing lsof on every tick — it
    asks exactly the question the helper's own bind asked, and costs microseconds. Note the
    inverted sense: if OUR bind SUCCEEDS the port was free, which means the leg we asked for
    is NOT there. Success here is the failure signal.

    Deliberately no SO_REUSEADDR/SO_REUSEPORT: with either set the bind can succeed
    alongside the helper's socket and we would report a healthy leg as missing.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


class LegWatch:
    """Polls the three media legs while a session is live and publishes the truth."""

    PERIOD_S = 3.0
    # Two consecutive misses before calling it. A single miss can be the helper rebinding
    # during a rate change, and a watchdog that cries wolf gets ignored — which is exactly
    # how we ended up not trusting the green lights in the first place.
    STRIKES = 2
    # Grace after a session goes live, before this is allowed to fire at all.
    #
    # SESSION.live flips as soon as ffmpeg is spawned, but the mesh helper binds its legs a
    # moment later — so for the first few seconds of EVERY healthy session the legs are
    # legitimately absent. Without this the watchdog fired during startup and tore the new
    # session down before it finished coming up, which is exactly what happened on the first
    # live run after it shipped. A watchdog must never be the reason the thing it guards dies.
    GRACE_S = 20.0

    def __init__(self):
        self.lock = threading.Lock()
        self.missing = []          # ports currently believed down
        self.since = 0.0           # when they went down
        self.strikes = 0
        self.drops = 0             # how many times this session lost a leg
        self.live_since = 0.0      # when the current session started (0 = not live)

    def snapshot(self):
        with self.lock:
            return {"ok": not self.missing, "missing": list(self.missing),
                    "down_for_s": round(time.time() - self.since, 1) if self.missing else 0,
                    "drops": self.drops}

    def reset(self):
        with self.lock:
            self.missing, self.since, self.strikes, self.drops = [], 0.0, 0, 0

    def _tick(self):
        # A DEAD HELPER IS NOT A QUIET SESSION.
        #
        # This used to read `if not SESSION.live or not MESH.proc or MESH.proc.poll() is not
        # None`, clearing the missing-leg list for all three. The first is correct -- no
        # session, nothing to watch. The other two are FAULTS being treated as calm: when the
        # mesh helper died, this cleared the alarm and `legs.ok` went TRUE. The app reported
        # itself healthier for having lost the thing that carries the room's audio back.
        #
        # Helper death is now reported by MESH.health() and surfaced in /api/state as `mesh`.
        # The watchdog still stops probing legs it cannot reach through a dead helper -- there
        # is nothing useful to say about them -- but it no longer pretends they are fine.
        if not SESSION.live:
            with self.lock:
                self.missing, self.strikes, self.live_since = [], 0, 0.0
            return
        if not MESH.proc or MESH.proc.poll() is not None:
            with self.lock:
                # Do not clear `missing`: the legs genuinely are unreachable. Freeze what we
                # know and let `mesh` explain why, rather than reporting an all-clear.
                self.strikes = 0
            return
        with self.lock:
            if not self.live_since:
                self.live_since = time.time()          # session just came up
            young = (time.time() - self.live_since) < self.GRACE_S
        if young:
            return                                     # still binding; not our business yet
        # The helper binds the forward ports locally; the RETURN port belongs
        # to GStreamer, not the helper (its return listener is inside tsnet).
        # Turning local playback off deliberately releases that port.
        expected = [RTP_VIDEO, RTP_VOICE]
        if SESSION.return_on:
            expected.append(SESSION.return_port)
        gone = [p for p in expected if not _leg_bound(p)]
        with self.lock:
            if gone:
                self.strikes += 1
                if self.strikes >= self.STRIKES and not self.missing:
                    self.missing = gone
                    self.since = time.time()
                    self.drops += 1
                    print("[legs] local UDP listener missing: %s (forward ports: mesh helper; return port: audio player)"
                          % ", ".join(str(p) for p in gone), flush=True)
                elif self.missing:
                    self.missing = gone
            else:
                if self.missing:
                    print("[legs] restored", flush=True)
                self.missing, self.strikes = [], 0

    def run(self):
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.PERIOD_S)


LEGS = LegWatch()


# ---------------------------------------------------------------- stream guard
#
# Watches the media legs every 5s and repairs the SMALLEST thing that is broken.
#
# The failure this exists for: SESSION.live is `any(p.poll() is None for p in procs)`, so if
# the VIDEO ffmpeg dies while voice is still running, the app happily reports LIVE and the
# presenter's camera is simply gone from the meeting with nothing said. Same for the return
# player. Nobody was watching individual legs.
#
# The rule it follows, learned the hard way tonight: NEVER repair upward. A dead video leg
# gets a new video leg — not a session rebuild. Tearing down the session to fix one leg
# causes a larger outage than the fault, and when the earlier leg-watchdog did exactly that
# it ended a live call and printed "session ended" as though the user had done it.
#
# What it deliberately does NOT touch: the mesh helper and the session itself. Those are
# LegWatch's business, they are genuinely interruptive to rebuild, and they need the human
# in the loop. This guard only ever restarts a process that has already died.

class StreamGuard:
    PERIOD_S = 5.0
    GRACE_S = 15.0        # legs need a moment after go-live; do not race startup
    MAX_PER_LEG = 5       # a leg that will not stay up is a real fault, not a blip

    def __init__(self):
        self.lock = threading.Lock()
        self.repairs = {}          # leg -> count of repairs this session
        self.last = None           # human-readable last action
        self.live_since = 0.0
        self.giving_up = []        # legs that exceeded MAX_PER_LEG
        self.waiting_for_mic = False

    def snapshot(self):
        with self.lock:
            return {"repairs": dict(self.repairs), "last": self.last,
                    "abandoned": list(self.giving_up),
                    "waiting_for_mic": self.waiting_for_mic}

    def _tick(self):
        # `wanted` first, and separately from `live`. A leg can still be dying while the
        # operator's stop is already in flight, and in that window `live` is briefly true - so
        # gating on `live` alone let one last repair through and printed "restart FAILED",
        # which reads like a fault when it was a correct refusal. If the session was ended,
        # there is nothing here to supervise.
        if not getattr(SESSION, "wanted", False):
            with self.lock:
                self.live_since = 0.0
                self.waiting_for_mic = False
                if self.repairs or self.giving_up:
                    self.repairs, self.giving_up, self.last = {}, [], None
            return
        with self.lock:
            if not self.live_since:
                self.live_since = time.time()
            if (time.time() - self.live_since) < self.GRACE_S:
                return
            abandoned = list(self.giving_up)

        for name, alive in (SESSION.leg_status() or {}).items():
            if name == "voice" and getattr(SESSION, "voice_muted", False) is True:
                continue
            if name == "voice" and getattr(SESSION, "voice_backend", None) == "gstreamer-coreaudio":
                from audio_engine import mac_microphone_device_id
                try:
                    current_device = mac_microphone_device_id(SESSION.voice_capture[0])
                except RuntimeError:
                    # An unplugged mic is not a crash loop. Keep watching without
                    # launching children or consuming the retry budget.
                    SESSION.suspend_voice()
                    with self.lock:
                        self.waiting_for_mic = True
                        self.last = "Microphone disconnected — waiting for " + SESSION.voice_capture[0]
                    continue
                # A fast unplug/replug can happen between polls while the old
                # process stays alive. Its CoreAudio object must not be reused.
                if current_device != getattr(SESSION, "voice_device_id", None):
                    SESSION.suspend_voice()
                    alive = False
                    with self.lock:
                        self.waiting_for_mic = True
                with self.lock:
                    if self.waiting_for_mic:
                        self.waiting_for_mic = False
                        self.repairs.pop("voice", None)
                        self.giving_up = [leg for leg in self.giving_up if leg != "voice"]
                        abandoned = [leg for leg in abandoned if leg != "voice"]
            if alive is not False or name in abandoned:
                continue          # True = healthy, None = deliberately off
            with self.lock:
                n = self.repairs.get(name, 0)
                if n >= self.MAX_PER_LEG:
                    if name not in self.giving_up:
                        self.giving_up.append(name)
                        self.last = ("%s leg failed %d times — not restarting again; "
                                     "End session and go live to rebuild" % (name, n))
                        print("[guard] %s" % self.last, flush=True)
                    continue
            ok = SESSION.set_return(True) if name == "return" else SESSION.respawn_leg(name)
            waiting = getattr(SESSION, "waiting_for_devices", {})
            if isinstance(waiting, dict) and name in waiting:
                with self.lock:
                    self.last = "Waiting for " + waiting[name]
                continue  # unplugging a selected device does not consume crash retries
            with self.lock:
                self.repairs[name] = self.repairs.get(name, 0) + 1
                self.last = "%s leg died -> %s (repair #%d)" % (
                    name, "restarted" if ok else "restart FAILED", self.repairs[name])
            print("[guard] %s" % self.last, flush=True)

    def run(self):
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.PERIOD_S)


GUARD = StreamGuard()


class BridgeWatch:
    """Ask the BRIDGE whether media is actually arriving, and repair what is not.

    LegWatch and StreamGuard only ever prove that THIS machine opened a socket and that a
    process is alive. Those are real checks — they catch the half-wired mesh — but they are
    blind to everything past the send() call. On 2026-08-11 the Mac's camera stopped opening
    and both of them reported `legs ok, drops 0` for the entire time the meeting saw a black
    tile: sockets bound, encoder process alive, not one frame leaving the machine. A green
    light that cannot go red is worse than no light, because it sends you looking in the
    wrong place — the same way the CI smoke test passed a build that could not reach the
    fleet.

    The bridge already answers the only question that matters. /api/checks measures the
    feeders' CPU ticks and the return stream's ALSA hardware pointer — that is BYTES ARE
    MOVING, observed at the far end, not inferred from this one. Nobody was asking it.

    Repairs are the smallest thing that could fix each symptom, and NEVER end the session:

        video / voice not arriving   ->  respawn that one encoder leg
        return audio not arriving    ->  re-issue set-peer (the presenter's mesh node is
                                         ephemeral and takes a new IP on every restart,
                                         which is exactly how the bridge ends up talking
                                         to an address that no longer exists)

    Deliberately reluctant: a grace period after go-live, two consecutive bad polls before
    acting, a cap per symptom, and — most importantly — it does NOTHING when the bridge
    cannot be reached. An unanswered poll means the CHECK failed, not the stream. Acting on
    absent evidence is how a supervisor tears down a working meeting, which is the precise
    failure this class exists to prevent.
    """

    PERIOD_S = 5.0         # includes the bridge's 2s sample, rather than adding to it
    REPAIR_PERIOD_S = 10.0 # faster display must not accelerate automatic repairs
    GRACE_S = 30.0         # longer than StreamGuard: the far end has to see traffic first
    STRIKES = 2            # one bad poll is a sample, two is a symptom
    MAX_REPAIRS = 3

    # bridge check -> the leg that would fix it
    LEG_FOR = {"video_arriving": "video", "voice_arriving": "voice"}

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last_repair_poll = None
        self.last_checks = None      # raw /api/checks, for the UI
        self.last_poll = 0.0
        self.reachable = None        # None = never polled this session
        self.strikes = {}
        self.repairs = {}
        self.last = None
        self.live_since = 0.0
        self.power = None
        self.give_up = set()   # symptoms a repair has proven it cannot fix

    def request_refresh(self):
        self.wake.set()

    def snapshot(self):
        with self.lock:
            return {"checks": self.last_checks, "reachable": self.reachable,
                    "power": self.power,
                    "age_s": round(time.time() - self.last_poll, 1) if self.last_poll else None,
                    "repairs": dict(self.repairs), "last": self.last}

    def _control_base(self):
        port = getattr(MESH, "control_port", None)
        return "http://127.0.0.1:%d" % port if port else None

    def _fetch(self):
        base = self._control_base()
        if not base:
            return None
        try:
            req = urllib.request.Request(base + "/api/checks")
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def _reset(self):
        with self.lock:
            self.live_since = 0.0
            self.last_repair_poll = None
            self.strikes, self.repairs, self.give_up = {}, {}, set()
            self.last_checks, self.reachable, self.last_poll = None, None, 0.0
            self.last = None
            self.power = None

    def _power(self):
        """The bridge's decoded power verdict, or None if it cannot be read.

        Read separately from /api/checks because a brownout is not a media fault and must
        not be repaired like one — it changes what the OTHER failures mean.
        """
        base = self._control_base()
        if not base:
            return None
        try:
            req = urllib.request.Request(base + "/api/status")
            with urllib.request.urlopen(req, timeout=8) as r:
                return (json.loads(r.read().decode("utf-8", "replace")) or {}).get("power")
        except Exception:
            return None

    def _apply_fleet_tuning(self, want):
        """Adopt return-audio tuning published by the fleet via the bridge.

        THE KNOB IS ON THIS MACHINE, NOT ON THE BRIDGE
        ----------------------------------------------
        Room audio is decoded here, through SESSION.return_jitter_ms. The bridge's
        rtpjitterbuffer is the opposite direction — the presenter's voice arriving there.
        So when an operator hears jitter, the buffer that absorbs it is local, and no fleet
        action could previously reach it: there is no inbound path to a laptop behind NAT.

        The bridge cannot push, but this class already polls it every 10 seconds. So the
        fleet writes the desired tuning on the bridge and it is picked up here within one
        poll — a fleet click that changes the presenter's audio mid-call, with nothing
        restarted on the bridge and no interruption to video.

        Applied at most once per (ts, values) change. Re-applying rebuilds the return
        pipeline, which costs about a second of room audio; doing that every 10s because a
        file merely exists would be its own fault. The bridge is a courier, so the values are
        treated as untrusted input and clamped by set_return_tuning (0-1000ms) — a bridge
        must not be able to make this machine do something unbounded.
        """
        if not isinstance(want, dict):
            return
        try:
            stamp = (want.get("ts"), want.get("jitter_ms"), want.get("conceal"),
                     want.get("gain"))
        except Exception:
            return
        if stamp == getattr(self, "_tuning_stamp", None):
            return
        jitter = None if getattr(SESSION, "return_jitter_manual", False) else want.get("jitter_ms")
        if jitter is None and want.get("conceal") is None and want.get("gain") is None:
            self._tuning_stamp = stamp
            return
        try:
            applied = SESSION.set_return_tuning(gain=want.get("gain"),
                                                jitter_ms=jitter,
                                                conceal=want.get("conceal"))
        except Exception as e:
            print("[fleet-tune] could not apply %r: %s" % (want, e), flush=True)
            return
        self._tuning_stamp = stamp
        why = want.get("reason") or "fleet request"
        with self.lock:
            self.last = "fleet tuning applied: return buffer %sms (%s)" % (
                applied.get("jitter_ms"), why)
        print("[fleet-tune] %s" % self.last, flush=True)

    def _giving_up_because(self, key, done):
        """Say which END the fault is on before telling anyone to go looking.

        The old text was always "check the bridge". On 2026-08-14 that sentence was printed
        while the actual fault was on THIS Mac: the camera daemons were wedged after a
        process was killed mid-capture, so avfoundation handed out the device and delivered
        no frames. The bridge was healthy — voice and return audio were green on the same
        poll — and the operator was sent to the far end of the link to look for it. That is
        the exact wrong-end misdiagnosis this whole supervisor exists to prevent.

        The tell is local and already recorded: a leg that keeps DYING is a capture failure
        on this machine, because a bridge that is not receiving cannot kill our encoder. A
        leg that stays up while the far end reports nothing arriving is a transport or bridge
        problem. Those need different sentences and different people.
        """
        leg = self.LEG_FOR.get(key)
        deaths = 0
        if leg:
            try:
                deaths = int((GUARD.snapshot() or {}).get("repairs", {}).get(leg, 0))
            except Exception:
                deaths = 0

        # A leg that is ALIVE but doing no work is the case the death-counting heuristic below
        # cannot see, and it is the one that actually happened: a wedged macOS camera leaves
        # ffmpeg running happily while avfoundation delivers nothing. Ask what it is doing, not
        # whether it exists.
        rate = SESSION.leg_cpu_rate(leg) if leg else None
        if leg == "video" and rate is not None and rate < SESSION.CPU_FLOOR:
            return ("LOCAL_CAMERA_FAULT: video is not arriving, and the encoder on THIS Mac is "
                    "running but doing almost no work (%.3f CPU-seconds per second, against "
                    "~0.3 for a real stream). The camera has been handed out but is delivering "
                    "no frames — the bridge cannot cause that. Fix it here:\n"
                    "    bash tools/fix-camera-macos.sh\n"
                    "then Stop and Go live again so the leg is rebuilt." % rate)
        if leg == "voice" and rate is not None and rate < SESSION.CPU_FLOOR:
            return ("LOCAL_MIC_FAULT: voice is not arriving and the capture process on THIS Mac "
                    "is idle (%.3f CPU-seconds per second). The microphone is open but "
                    "producing nothing; re-select it in the app." % rate)

        # Other checks green on the same poll means the link and the bridge are fine.
        others_ok = [k for k, v in (self.last_checks or {}).items()
                     if isinstance(v, dict) and k != key
                     and k in ("video_arriving", "voice_arriving", "return_audio")
                     and v.get("ok")]

        if leg == "video" and deaths and others_ok:
            return ("video is not arriving, but the video leg keeps dying on THIS Mac while "
                    "%s still work — so the bridge is fine. This is almost always the macOS "
                    "camera left wedged by a process that was killed while holding it: it "
                    "opens but delivers no frames. Fix it here, not on the bridge:\n"
                    "    bash tools/fix-camera-macos.sh" % " and ".join(others_ok))
        if leg and deaths:
            return ("%s is not arriving and the %s leg keeps dying on THIS Mac (%d times) — "
                    "the fault is local capture or encoding, not the bridge."
                    % (key, leg, deaths))
        return ("%s still failing after %d repairs, and the leg here is staying up — so the "
                "problem is downstream of this Mac: the network or the bridge." % (key, done))

    def _repair(self, key, checks):
        """Fix one failing check. Returns a human sentence, or None if nothing was done."""
        leg = self.LEG_FOR.get(key)
        if leg:
            ok = SESSION.respawn_leg(leg)
            return "%s not arriving at the bridge -> restarted the %s leg%s" % (
                leg, leg, "" if ok else " (restart FAILED)")

        if key == "return_audio":
            # The bridge is sending room audio to an address that is probably stale. Tell it
            # where we are NOW rather than restarting anything — an ephemeral mesh node takes
            # a fresh IP on every app start, so this is a re-address, not a fault.
            me = getattr(MESH, "tailnet_ip", None)
            base = self._control_base()
            if not (me and base):
                return None
            try:
                req = urllib.request.Request(
                    base + "/api/set-peer",
                    data=json.dumps({"ip": me, "port": SESSION.return_port or 5004,
                                     "ticket": PINS.ticket() or ""}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    res = json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    try:
                        reason = json.loads(e.read().decode()).get("reason") or "locked"
                    except Exception:
                        reason = "locked"
                    PINS.clear(lost=reason)
                    return SESSION_LOST.get(reason, SESSION_LOST["locked"])
                return "room audio was not coming back -> could not re-point the bridge (%s)" % e
            except Exception as e:
                return "room audio was not coming back -> could not re-point the bridge (%s)" % e

            if not res.get("changed"):
                # The bridge was ALREADY pointed at us, so this repair changed nothing and
                # repeating it never will. Observed 2026-08-12: return audio was red because
                # the client laptop was not capturing its microphone — a fault on the far side
                # of the USB cable that no peer address can fix — and this loop re-issued
                # set-peer every tick, logging "(already correct)" each time.
                #
                # Stop attempting it and say what is actually wrong. A supervisor that keeps
                # applying a fix it can see had no effect is just noise, and it hid the real
                # cause behind its own chatter.
                self.give_up.add("return_audio")
                return ("room audio is not coming back; the bridge destination is already %s. "
                        "That does not verify packet delivery. Check that the meeting laptop "
                        "plays to the NetBridge speaker, then check USB capture, mesh delivery "
                        "and the local return player." % me)
            return "room audio was not coming back -> re-pointed the bridge at %s" % me
        return None

    def _tick(self):
        # Same reasoning as StreamGuard: an ended session is not a fault to diagnose, and
        # repairing one would undo the operator's stop.
        if not getattr(SESSION, "wanted", False) or not SESSION.live:
            if self.live_since or self.last_checks:
                self._reset()
            return

        with self.lock:
            if not self.live_since:
                self.live_since = time.time()

        checks = self._fetch()
        with self.lock:
            self.last_poll = time.time()
            self.reachable = checks is not None
            if checks:
                self.last_checks = checks

        if not checks:
            # Say so, but do not act. The stream may be perfectly fine.
            return

        # The bridge ended our PIN session (10 min without video, 12 h, an admin lock, a reboot,
        # someone else's PIN). It now refuses our media, so every "repair" would be noise: drop
        # the ticket, say why, and let the page ask for the PIN.
        pin = checks.get("pin") or {}
        ticket = PINS.ticket()
        epoch = (pin.get("session") or {}).get("video_epoch")
        replaced = bool(ticket and epoch and epoch != hashlib.sha256(ticket.encode()).hexdigest()[:16])
        if replaced or (pin.get("locked") and int(pin.get("protocol") or 1) >= PIN_PROTOCOL):
            ended = pin.get("last_end") or "locked"
            reason = "superseded" if replaced else ended.get("reason", "locked") if isinstance(ended, dict) else ended
            message = SESSION_LOST.get(reason, SESSION_LOST["locked"])
            PINS.clear(lost=reason)
            SESSION.stop()
            SESSION.interruption = message
            with self.lock:
                self.last = message
            return

        # Publish measurements during startup; only corrective actions need grace.
        now = time.monotonic()
        with self.lock:
            if (time.time() - self.live_since) < self.GRACE_S:
                return
            if self.last_repair_poll is not None and now - self.last_repair_poll < self.REPAIR_PERIOD_S:
                return
            self.last_repair_poll = now

        # Adopt any tuning the fleet has asked this machine for. Runs BEFORE the repair logic
        # below so a deliberate operator change is never mistaken for a symptom.
        self._apply_fleet_tuning(checks.get("presenter_tuning"))

        # A browning-out board changes what every other symptom MEANS.
        #
        # Under-voltage throttles the SoC to a crawl. Packets are not lost — they arrive
        # late, in bursts — so the media legs are healthy and restarting one fixes nothing
        # while costing a real gap in the meeting. Worse, the repair itself is a CPU spike
        # on a board that is already short of current, so the supervisor would be feeding
        # the fault it is trying to fix.
        #
        # Proven on this hardware: a flight recording caught 327 separate live brownout
        # episodes, and the board rebooted mid-session. The audible result was jitter that
        # looked exactly like a network problem and was chased as one for hours.
        #
        # So when the bridge reports a brownout, say so in plain words and stand down. The
        # honest message is worth more than a repair that cannot work.
        power = self._power()
        if isinstance(power, dict) and power.get("live"):
            with self.lock:
                self.last = ("the bridge reports under-voltage — media disruption or a reboot "
                             "is possible. Check the power path; this does not prove the "
                             "cause of an audible fault. Not restarting anything.")
                self.power = power
            print("[bridge] %s" % self.last, flush=True)
            return

        with self.lock:
            self.power = power if isinstance(power, dict) else None

        video_check = checks.get("video_arriving") or {}
        quality_note = SESSION.adapt_video(video_check, now) if video_check.get("source_state") == "live" else None
        if quality_note:
            with self.lock:
                self.last = quality_note
            return  # never combine a quality change with another repair in the same tick

        for key, val in checks.items():
            if key == "voice_arriving" and getattr(SESSION, "voice_muted", False) is True:
                with self.lock:
                    self.strikes[key] = 0
                continue
            if not isinstance(val, dict) or key not in ("video_arriving", "voice_arriving",
                                                        "return_audio"):
                continue
            if val.get("ok"):
                with self.lock:
                    self.strikes[key] = 0
                    self.give_up.discard(key)      # it recovered; allow repairs again
                continue
            with self.lock:
                if key in self.give_up:
                    continue                        # proven unfixable from here — stay quiet

            with self.lock:
                self.strikes[key] = self.strikes.get(key, 0) + 1
                n, done = self.strikes[key], self.repairs.get(key, 0)
            if n < self.STRIKES or done >= self.MAX_REPAIRS:
                if done >= self.MAX_REPAIRS and n == self.STRIKES:
                    with self.lock:
                        self.last = self._giving_up_because(key, done)
                    print("[bridge] %s" % self.last, flush=True)
                continue

            note = self._repair(key, checks)
            if not note:
                continue
            with self.lock:
                self.repairs[key] = done + 1
                self.strikes[key] = 0
                self.last = note
            print("[bridge] %s" % note, flush=True)

    def run(self):
        while True:
            self.wake.clear()
            started = time.monotonic()
            try:
                self._tick()
            except Exception:
                pass
            # One request at a time; slow samples never overlap or queue up.
            self.wake.wait(max(0.1, self.PERIOD_S - (time.monotonic() - started)))


BRIDGEWATCH = BridgeWatch()


def _mesh_hostname(rec, st):
    """Name the presenter's mesh node after WHO is connecting and to WHAT.

    It used to be "nb-source-<last6 of the BRIDGE id>" — named after the bridge, not the
    person. With one bridge and one presenter that is invisible. With several it is a mess:
    two presenters on the same bridge both request the SAME hostname and Tailscale
    disambiguates them as -1 and -2, while one presenter on two bridges appears under two
    unrelated names. Either way you cannot tell from the node list who is connected to what.

    Now: nb-<user>-<pairing code>, e.g. nb-samith-2626.

    TO OVERRIDE, either:
      * set NB_MESH_NAME=<something>   (env, wins over everything), or
      * add "mesh_name": "<something>" to ~/.netbridge-source/state.json
    Both are sanitised to what Tailscale accepts for a hostname (a-z 0-9 and dashes).
    """
    def clean(v, n):
        v = re.sub(r"[^a-zA-Z0-9-]+", "-", str(v or "")).strip("-").lower()
        return v[:n]

    override = os.environ.get("NB_MESH_NAME") or st.get("mesh_name")
    if override:
        return clean(override, 60) or "nb-source"

    # who: the local part of the signed-in email, which is the person's own name to them
    who = clean((st.get("email") or "").split("@")[0], 20) or "source"
    # what: the pairing code is what is printed on the bridge's label, so it is the name a
    # human already associates with that box — better than 6 hex digits of an internal id
    code = clean((rec or {}).get("pairing_code", "").replace("BRIDGE-", ""), 8) \
        or clean(((rec or {}).get("id") or "")[-6:], 8) or "bridge"
    return "nb-%s-%s" % (who, code)


def _uptime_seconds(s):
    """Parse the bridge's human uptime ('1 minute', '2 hours 5 minutes') to seconds.

    Only ever used to notice the value going BACKWARDS, which means the bridge rebooted.
    A wrong parse would at worst miss a reboot, never invent one — so returning None on
    anything unexpected is the safe failure, and the caller treats None as 'no opinion'."""
    if not isinstance(s, str):
        return None
    total, found = 0, False
    for n, unit in re.findall(r"(\d+)\s*(second|minute|hour|day)", s):
        total += int(n) * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]
        found = True
    return total if found else (0 if "less than" in s else None)


def _bridge_rec(host, st):
    """Find the bridge record for a UI-supplied host (its tailnet or LAN IP). Falls back
    to a bare {ip:host} so a hand-typed address still works."""
    lst = _BRIDGES["list"]
    if not lst and st.get("token") and st.get("control_url"):
        r = _fleet_bridges(st)
        if isinstance(r, list):
            lst = _BRIDGES["list"] = r
    for d in lst:
        if host in (d.get("id"), d.get("tailscale_ip"), (d.get("latest") or {}).get("ip"), d.get("ip")):
            return {"id": d.get("id"), "tailscale_ip": d.get("tailscale_ip"),
                    "ip": (d.get("latest") or {}).get("ip") or d.get("ip")}
    return {"id": None, "tailscale_ip": None, "ip": host}


def bridge_route(host, st, read_only=False):
    """Base control URL + routing for a bridge, bringing the mesh up if appropriate.

    via="none" means the mesh could not be established. There is deliberately no LAN
    fallback, so there is nothing to build a base URL from — callers must check `error`
    rather than dereference control_host, which would otherwise raise a TypeError and
    surface as a stack trace instead of the plain reason the route failed."""
    rec = _bridge_rec(host, st)
    if read_only:
        # Status polling must never create/repoint a helper or interrupt a live session.
        if (MESH.proc is not None and MESH.proc.poll() is None
                and MESH.bridge_id == rec.get("id") and MESH.bridge_id is not None):
            route = MESH._mesh_route()
        else:
            return {"via": "none", "base": "",
                    "error": "No active mesh route for this bridge; start its session first."}
    else:
        route = MESH.route(rec, st)
    if route.get("via") == "none":
        route["base"] = ""
        return route
    route["base"] = "http://%s:%d" % (route["control_host"], route["control_port"])
    return route


def support_report():
    snapshot = BRIDGEWATCH.snapshot()
    checks = snapshot.get("checks") or {}
    fresh = snapshot.get("reachable") is not False and snapshot.get("age_s") is not None and snapshot["age_s"] <= 20
    return {"app_version":APP_VERSION,"platform":"macOS" if IS_MAC else "Windows",
            "live":bool(SESSION.live),"wanted":bool(SESSION.wanted),"return_on":bool(SESSION.return_on),
            "delivery":{k:(checks[k].get("ok") if fresh and isinstance(checks.get(k),dict) else None)
                        for k in ("video_arriving","voice_arriving","return_audio","client_sees_camera")}}


def setup_check(selection):
    """Inspect prerequisites without starting media, changing a route or recording audio."""
    st = load_state()
    devices = av_devices() if not SESSION.live else {"video":[], "audio":[]}
    rows = []
    def row(key, label, status, detail):
        rows.append({"key":key,"label":label,"status":status,"detail":detail})
    row("signin","Sign-in","pass" if st.get("token") else "fail",
        "Signed in locally; Fleet access is checked below." if st.get("token") else "Sign in with your work email.")
    permission = ("System Settings → Privacy & Security → Camera and Microphone. Allow the application used to launch NetBridge."
                  if IS_MAC else "Settings → Privacy & security → Camera and Microphone. Check access for desktop apps and any organisation restrictions.")
    for key,label,field in (("video","Camera","camera_name"),("audio","Microphone","mic_name")):
        names = [d.get("name") for d in devices.get(key,[])]
        wanted = selection.get(field) or st.get(field)
        system_mic = key == "audio" and IS_MAC
        if SESSION.live:
            row(key,label,"unknown","A session is active; setup does not open or replace its capture devices.")
        else:
            found = bool(names) and (system_mic or wanted in names)
            row(key,label,"pass" if found else "warn",
                "Selected device is listed. Capture permission and actual delivery are verified when started." if found else
                "Select an available device. If none appears, check " + permission)
    bridge_id = selection.get("bridge_id") or st.get("bridge_id")
    bridges = _fleet_bridges(st) if st.get("token") else []
    if not isinstance(bridges,list):
        row("fleet","Fleet connection","unknown","Fleet could not be reached. Check your internet connection and sign-in.")
    else:
        bridge=next((b for b in bridges if b.get("id")==bridge_id),None)
        row("bridge","Selected bridge","pass" if bridge else "fail",
            "Bridge belongs to your available fleet." if bridge else "Choose a bridge from your organisation.")
        if bridge:
            row("availability","Bridge availability","pass" if bridge.get("online") else "unknown",
                "Fleet has recent telemetry; this does not test the media path." if bridge.get("online") else "Fleet telemetry is stale. Actual mesh reachability is not yet tested.")
            row("maintenance","Maintenance","warn" if bridge.get("maintenance") else "pass",
                "Contact your administrator: scheduled maintenance is active." if bridge.get("maintenance") else "No maintenance window reported.")
            protocol=bridge.get("pin_protocol")
            if type(protocol) is not int: protocol=None
            row("compatibility","App / bridge compatibility","pass" if protocol and protocol>=2 else "unknown" if protocol is None else "fail",
                "Bridge supports this app's PIN protocol." if protocol and protocol>=2 else "This app needs a protocol-2 bridge. Ask the administrator to confirm a compatible release.")
    row("receiver","Meeting laptop picture","unknown","After starting, ask the room to select NetBridge in its meeting app and confirm the picture. USB status cannot verify the displayed image.")
    return {"checks":rows,"checked_at":time.time(),"status":"needs_attention" if any(r["status"] in ("warn","fail") for r in rows) else "partially_verified"}


# --------------------------------------------------------------------------- server
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer   # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                    # keep the presenter's terminal quiet

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Same reason the fleet panel is no-store: this UI is regenerated by every app
        # update, and a browser holding an old copy makes a shipped fix look like a fix that
        # did not work. Localhost, one small page - revalidating always is free.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # This app talks to the local machine only; no page anywhere may script it.
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """Parse the JSON body — ONCE per request, cached.

        A request body can only be read off the socket once. do_POST reads it up front, so
        any handler that called _body() again blocked in rfile.read() waiting for bytes that
        had already been consumed — the request then hung until the client gave up. That is
        exactly what "Play meeting audio here" did: the toggle appeared dead, and the return
        player was never actually stopped or started. Caching makes a second call free and
        correct instead of fatal."""
        if getattr(self, "_body_cache", None) is None:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                self._body_cache = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._body_cache = {}
        return self._body_cache

    def _local_host_ok(self):
        # DNS rebinding can turn an attacker-controlled hostname into 127.0.0.1.
        # Validate GET as well as POST: diagnostics and recordings are private.
        values = self.headers.get_all("Host") if hasattr(self.headers,"get_all") else [self.headers.get("Host", "")]
        return bool(values and len(values) == 1 and re.fullmatch(
            r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", values[0], re.IGNORECASE))

    # ---------------- GET
    def do_GET(self):
        if not self._local_host_ok():
            return self._send({"_error": "Unrecognised local address."}, 403)
        self._body_cache = None
        if self.path == "/api/audio/diagnostics":
            return self._send(SESSION.audio_snapshot())
        match = re.fullmatch(r"/api/audio/captures/([a-f0-9]{32})/(decoded.wav|input.rtpdump|manifest.json|timing.jsonl)", self.path)
        if match:
            path = _logdir() / "captures" / match[1] / match[2]
            try:
                data = path.read_bytes()
                return self._send(data, ctype="audio/wav" if match[2].endswith(".wav") else "application/octet-stream")
            except OSError:
                return self._send({"error": "Capture file unavailable"}, 404)
        if self.path == "/":
            return self._send(UI.encode(), ctype="text/html; charset=utf-8")
        st = load_state()
        if self.path == "/api/state":
            return self._send({
                "signed_in": bool(st.get("token")),
                "email": st.get("email"),
                "control_url": st.get("control_url", ""),
                "last_bridge": st.get("bridge_id"),
                "last_camera": st.get("camera_name"),
                "last_mic": "System default microphone" if IS_MAC else st.get("mic_name"),
                "system_default_mic": IS_MAC,
                "voice_backend": getattr(SESSION, "voice_backend", None),
                "live": SESSION.live,
                "wanted": SESSION.wanted,
                "interruption": getattr(SESSION, "interruption", None),
                "live_host": (PINS.host() or st.get("bridge_host")) if SESSION.wanted else None,
                "live_bridge_id": MESH.bridge_id if SESSION.wanted else None,
                "pin": PINS.snapshot(),      # never the ticket itself
                "voice_muted": SESSION.voice_muted and not SESSION.voice_sending(),
                "legs": LEGS.snapshot(),
                # Reported SEPARATELY from legs, because a dead helper used to make the legs
                # look healthy. The UI must be able to say APP OK / MESH OK / BRIDGE OK /
                # RETURN AUDIO OK independently — one green light covering four things is how
                # a presenter ends up talking to a room that cannot answer.
                "mesh": MESH.health(),
                "guard": GUARD.snapshot(),
                "bridge_checks": BRIDGEWATCH.snapshot(),
                "return_jitter_manual": SESSION.return_jitter_manual,
                "return_on": SESSION.return_on, "return_gain": SESSION.return_gain, "return_dynamics": SESSION.return_dynamics, "return_jitter_ms": SESSION.return_jitter_ms, "return_sink_sync": SESSION.return_sink_sync, "return_conceal": SESSION.return_conceal, "return_fec": SESSION.return_fec, "return_plc": SESSION.return_plc,
                "version": APP_VERSION,
                "update_note": _update_note,
            })
        if self.path == "/api/devices":
            return self._send(av_devices())
        if self.path == "/api/bridges":
            if not st.get("token"):
                return self._send({"_error": "not signed in"}, 401)
            r = _fleet_bridges(st)
            if isinstance(r, list):
                _BRIDGES["list"] = r     # cache for mesh routing (tailscale_ip per bridge)
            if isinstance(r, dict) and r.get("_error"):
                return self._send(r, 401 if r.get("_code") == 401 else 502)
            # presenters see their org's bridges; never any secret
            out = [{"id": d.get("id"), "number": d.get("number"), "label": d.get("label"),
                    "name": d.get("name") or d.get("pairing_code"),
                    "pairing_code": d.get("pairing_code"), "online": d.get("online"),
                    "ip": d.get("ip") or (d.get("latest") or {}).get("ip"),
                    "tailscale_ip": d.get("tailscale_ip")} for d in (r or [])]
            # The fleet's "online" is a HEARTBEAT age, and the heartbeat travels a completely
            # different path (device -> control plane over the internet) from the one that
            # actually matters to a presenter (app -> device over the mesh). Those fail
            # independently: on 2026-07-31 a bridge sat healthy and mesh-reachable for 18
            # minutes while its telemetry path was down, and the app labelled it "(offline)"
            # and talked the presenter out of going live to a device that would have worked.
            #
            # So when the fleet says offline, ASK THE DEVICE. If it answers, it is online for
            # our purposes — a stale record must never veto a working bridge.
            for b in out:
                if b.get("online"):
                    continue
                if _bridge_reachable(b.get("id") or "", st):
                    b["online"] = True
                    b["online_via"] = "probe"     # fleet says stale; the device itself answered
            return self._send(out)
        if self.path.startswith("/api/checks"):
            if not self._csrf_ok() or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self._send({"_error": "cross-origin checks refused"}, 403)
            host = self.path.split("host=", 1)[1] if "host=" in self.path else ""
            if not host:
                return self._send({"_error": "host required"}, 400)
            route = bridge_route(host, load_state(), read_only=True)
            if route.get("via") == "none":
                return self._send({"_error": route.get("error", "no route to the bridge")}, 502)
            r = api("GET", route["base"] + "/api/checks", timeout=10)
            # Attach the leg state to the SAME response the UI already polls, so the front
            # end can tell "your camera is busy" apart from "this app is not sending
            # anywhere" without a second round trip.
            if isinstance(r, dict):
                r["_legs"] = LEGS.snapshot()
                r["_guard"] = GUARD.snapshot()
                # Bridge uptime, so the UI can spot a REBOOT. A rebooted bridge leaves the
                # session dead but green-LOOKING: our ffmpeg keeps sending to a device that
                # no longer holds the matching state, and the local checks all still pass.
                # It happened repeatedly tonight and each time cost a manual End -> Go live
                # that the presenter had to work out for themselves.
                s2 = api("GET", route["base"] + "/api/status", timeout=6)
                if isinstance(s2, dict) and not s2.get("_error"):
                    r["_bridge_uptime_s"] = _uptime_seconds(s2.get("uptime"))
                    # direct vs DERP relay — on a long link this decides whether the call
                    # is usable, and it was previously invisible without a terminal.
                    r["_mesh_path"] = s2.get("mesh_path") or {}
            if isinstance(r, dict) and "voice_arriving" not in r and not r.get("_error"):
                # Older bridge firmware does not measure the forward-voice leg. Fall back to
                # what THIS app can see: the mic->bridge ffmpeg leg is alive and sending.
                # Weaker than the device confirming receipt, and labelled as such.
                r["voice_arriving"] = {
                    "ok": SESSION.voice_sending(),
                    "detail": ("mic leg sending (bridge firmware pre-dates the receive check)"
                               if SESSION.voice_sending() else "voice leg not running")}
            return self._send(r)
        return self._send({"_error": "not found"}, 404)

    # Origins allowed to make state-changing calls: only this app's own page.
    _SELF_ORIGINS = ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT,
                     "http://[::1]:%d" % PORT)

    def _csrf_ok(self):
        """Reject state-changing calls that did not come from this app's own page.

        127.0.0.1 is NOT an authorization boundary. Every browser on this machine can reach
        this server, so any page the presenter has open in any tab could POST here — and
        until this check existed, that worked. One line on a hostile page:

            fetch('http://127.0.0.1:8765/api/stop', {method:'POST',
                  headers:{'Content-Type':'text/plain'}, body:'{}', mode:'no-cors'})

        ends a live meeting. text/plain is a CORS-safelisted content type, so the browser
        sends it with no preflight; the attacker cannot read the reply and does not need to,
        because the side effect IS the attack. /api/unlock was worse — three forged calls
        with wrong PINs lock the bridge out for an hour.

        Two gates, either sufficient on its own:

        1. If an Origin (or Referer) header is present it must be ours. Browsers always
           attach Origin to cross-origin POSTs and cannot be talked out of it.
        2. The body must be declared application/json, which is NOT CORS-safelisted. That
           forces a preflight, and this server answers none — so the browser never sends it.

        curl and other non-browser clients send neither header and are unaffected. That is
        deliberate: the threat is a hostile PAGE, not a local process. Anything already
        running as this user could reach the app regardless, so blocking curl would cost
        real usability and buy no security.
        """
        origin = self.headers.get("Origin")
        if origin and origin not in self._SELF_ORIGINS:
            return False
        if not origin:
            ref = self.headers.get("Referer") or ""
            if ref and not any(ref == o or ref.startswith(o + "/") for o in self._SELF_ORIGINS):
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return False
        return True

    # ---------------- POST
    def do_POST(self):
        if not self._local_host_ok():
            return self._send({"_error": "Unrecognised local address."}, 403)
        self._body_cache = None      # fresh per request (connections are reused)
        if not self._csrf_ok():
            return self._send({"_error": "cross-origin request refused; NetBridge only "
                                         "accepts calls from its own page"}, 403)
        st, b = load_state(), self._body()

        if self.path == "/api/signin-request":
            try:
                url = _signin_url(b.get("control_url"))
            except ValueError as exc:
                return self._send({"_error": str(exc)}, 400)
            email = (b.get("email") or "").strip()
            if not email:
                return self._send({"_error": "Enter your work email address."}, 400)
            r = api("POST", url + "/auth/magic-link", body={"email": email})
            if not isinstance(r, dict) or r.get("_error") or r.get("ok") is not True:
                return self._send({"_error": _signin_error(r if isinstance(r,dict) else {})}, 502)
            st["control_url"] = url
            save_state(st)
            return self._send({"ok": True, "note": "If that address has an account, a sign-in code is on its way."})

        if self.path == "/api/signin-redeem":
            try:
                url = _signin_url(b.get("control_url") or st.get("control_url"))
            except ValueError as exc:
                return self._send({"_error": str(exc)}, 400)
            code = (b.get("code") or "").strip()
            if not code:
                return self._send({"_error": "Enter your sign-in or invite code."}, 400)
            r = _redeem_signin(url, code)
            tok = (r or {}).get("token")
            if not tok:
                return self._send({"_error": _signin_error(r, redeem=True)}, 401 if (r or {}).get("_code") in (401,403) else 502)
            st["control_url"] = url
            st["token"] = tok
            # /auth/redeem already tells us who we are; whoami is a fallback so the header
            # never shows a blank identity after a successful sign-in.
            who = api("GET", url + "/auth/whoami", token=tok)
            st["email"] = (who or {}).get("email") or (r or {}).get("email")
            save_state(st)
            return self._send({"ok": True, "email": st["email"]})

        if self.path == "/api/signout":
            # Signing out releases camera/microphone and cancels media recovery before
            # the page loses its controls. Closing the mesh alone leaves capture alive.
            SESSION.stop()
            # Drop the identity, keep control_url and the remembered devices: a forced
            # sign-out should not make the presenter re-pick their camera and mic.
            for k in ("token", "email"):
                st.pop(k, None)
            save_state(st)
            _end_bridge_session()
            MESH.stop()
            return self._send({"ok": True})

        if self.path == "/api/support-report":
            request = self._body()
            report = support_report()
            if not request.get("submit"):
                return self._send({"report":report,"notice":"Submitting shares these measurements, your account identity and a bridge-health snapshot with your fleet administrator. No recordings or raw logs are uploaded."})
            st = load_state()
            if not st.get("token"):
                return self._send({"_error":"Sign in before submitting a report"},401)
            result = api("POST",st.get("control_url", "https://fleet.scine.online").rstrip("/")+"/auth/support-reports",token=st["token"],
                         body={"device_id":request.get("bridge_id") or st.get("bridge_id"),"symptom":request.get("symptom","other"),"report":report})
            return self._send(result,502 if isinstance(result,dict) and result.get("_error") else 200)
        if self.path == "/api/preflight":
            return self._send(setup_check(self._body()))
        if self.path == "/api/remember":
            for k in ("bridge_id", "camera_name", "mic_name"):
                if b.get(k) is not None:
                    st[k] = b[k]
            save_state(st)
            return self._send({"ok": True})

        if self.path == "/api/unlock":
            # Studio unlocks first and then calls /api/golive without a PIN; the browser page
            # sends the PIN WITH go-live. Either way the PIN is checked on the bridge and only a
            # ticket comes back - kept in memory here, never handed to the page.
            host, pin = b.get("host"), str(b.get("pin") or "")
            if not host or not pin:
                return self._send({"_error": "host and pin required"}, 400)
            res = _unlock_bridge(host, st, pin)
            if res.get("ok") or res.get("reason") in PIN_MESSAGES:
                return self._send(res)               # unlocked, or a real PIN verdict
            return self._send(dict(res, _error=res.get("message")), 502)

        if self.path == "/api/golive":
            if _SUSPENDING.is_set():
                return self._send({"_error": "Wait for the computer to finish waking before presenting."}, 409)
            host = b.get("host")
            if not host:
                return self._send({"_error": "host required"}, 400)
            # Idempotent for the impatient. A second click while a session is already up
            # used to run the whole start path again — tearing down working legs and
            # rebuilding them, which a presenter experiences as the stream dropping for no
            # reason at the exact moment they were trying to fix something. Answer the
            # request truthfully instead of doing damage.
            if SESSION.live and SESSION.bridge:
                if b.get("pin"):
                    # The bridge relocked mid-session (10 min without video, an admin lock,
                    # someone else's PIN) while this app kept its media running. Re-open the
                    # session in place: the same mesh helper, so the gate admits us again the
                    # moment the PIN is accepted - no media restart, no new helper.
                    res = _unlock_bridge(host, st, str(b.get("pin")))
                    if not res.get("ok"):
                        return self._send(dict(res, _error=res.get("message")),
                                          401 if res.get("reason") in PIN_MESSAGES else 502)
                    route = bridge_route(host, st)
                    peer = api("POST", route["base"] + "/api/set-peer",
                               body={"ip": route["return_peer"], "port": SESSION.return_port or 5004,
                                     "ticket": PINS.ticket(host)}, timeout=15) if route.get("via") != "none" and route.get("return_peer") else None
                    if not isinstance(peer, dict) or peer.get("ok") is not True:
                        return self._send({"_error":"The bridge did not confirm the resumed session. Existing media has not been restarted."},502)
                    BRIDGEWATCH.request_refresh()
                    return self._send({"ok": True, "already_live": True, "resumed": True,
                                       "camera": st.get("camera_name"), "mic": st.get("mic_name"),
                                       "note": "PIN accepted — the bridge is taking your video again"})
                return self._send({"ok": True, "already_live": True,
                                   "camera": st.get("camera_name"), "mic": st.get("mic_name"),
                                   "note": "already live — nothing to do"})
            devs = av_devices()
            vidx, vname, found_video = resolve_by_name(devs.get("video", []), b.get("camera_name"))
            if b.get("camera_name") and not found_video:
                return self._send({"_error": "Selected camera is unavailable. Reconnect it or select another camera."}, 409)
            if IS_MAC and _gst():
                # Follow macOS input preference, including USB disconnect/reconnect.
                aidx, aname = "default", "System default microphone"
            else:
                aidx, aname, found_audio = resolve_by_name(devs.get("audio", []), b.get("mic_name"), "0")
                if b.get("mic_name") and not found_audio:
                    return self._send({"_error": "Selected microphone is unavailable. Reconnect it or select another microphone."}, 409)
            port = int(b.get("return_port") or 5004)
            # THE PIN. A go-live from the page carries it; Studio unlocked a moment ago; an
            # internal reconnect (reconnect=true) reuses this session's ticket. Nothing else
            # gets past this point - a bridge in the list is not permission to stream to it.
            pin = str(b.get("pin") or "")
            if pin:
                res = _unlock_bridge(host, st, pin)
                if not res.get("ok"):
                    return self._send(dict(res, _error=res.get("message")),
                                      401 if res.get("reason") in PIN_MESSAGES else 502)
            ticket = PINS.ticket(host)
            if not ticket:
                return self._send({"_error": SESSION_LOST.get(PINS.lost) or "Enter the bridge PIN to go live.",
                                   "need_pin": True, "reason": PINS.lost or "need_pin"}, 401)
            # Route over the mesh when the bridge has a tailnet address. Media then targets
            # 127.0.0.1 (the helper's local proxies) and the bridge is told to return audio
            # to OUR mesh IP - so no 100.x address is ever handled by the app itself.
            route = bridge_route(host, st)
            if route.get("via") == "none":
                # Refuse in plain words rather than starting ffmpeg into nothing. Go-live
                # reporting ok=True while media went nowhere is the single failure this
                # project has repeated most.
                return self._send({"_error": route.get("error", "no route to the bridge")}, 502)
            me = route["return_peer"]
            peer = api("POST", route["base"] + "/api/set-peer",
                       body={"ip": me, "port": port, "ticket": ticket}, timeout=15) if me else {"_error": "no route"}
            if isinstance(peer, dict) and peer.get("_code") == 401:
                # The bridge no longer honours this ticket (it rebooted, relocked after 10 min
                # without video, an admin locked it, or someone else entered the PIN). Do not
                # start media into a closed gate: ask for the PIN.
                reason = peer.get("_reason") or "locked"
                PINS.clear(lost=reason)
                return self._send({"_error": SESSION_LOST.get(reason, SESSION_LOST["locked"]),
                                   "need_pin": True, "reason": reason}, 401)
            if not isinstance(peer, dict) or peer.get("ok") is not True:
                return self._send({"_error":"The bridge did not confirm your session. Media has not been started; try again."},502)
            try:
                SESSION.start(route["media_host"], vidx, aidx, return_port=port, mic_name=aname)
            except RuntimeError as exc:
                return self._send({"_error": str(exc)}, 409)
            SESSION.capture_names = {"video": vname, "voice": aname}
            st.update({"bridge_host": host, "camera_name": vname, "mic_name": aname})
            save_state(st)
            BRIDGEWATCH.request_refresh()
            player = SESSION.return_player
            return self._send({"ok": True, "camera": vname, "mic": aname,
                               "return_peer": me, "return_port": port, "peer_result": peer,
                               "via": route.get("via"), "return_player": player,
                               "return_note": {
                                   "gstreamer-persistent": "room audio: persistent GStreamer receiver",
                                   "gstreamer": "room audio: GStreamer (best — jitter-buffered)",
                                   "ffmpeg": "room audio: ffmpeg fallback — install GStreamer for smoother playback",
                                   "none": "NO ROOM AUDIO — neither GStreamer nor ffmpeg could start a player",
                               }.get(player, player)})

        if self.path == "/api/stop":
            # keep_session=true is the page reconnecting on its own (a media leg dropped, the
            # bridge rebooted): same session, so the ticket stays. A presenter's Stop ends the
            # bridge's session (its gate closes at once) and forgets the ticket.
            keep = bool(b.get("keep_session")) and bool(PINS.ticket())
            ended = False if keep else _end_bridge_session()
            SESSION.stop()
            MESH.stop()
            return self._send({"ok": True, "session_kept": keep, "bridge_session_ended": ended})

        if self.path == "/api/return-tuning":
            # Live return-audio tuning. Both documented artifacts are fixed from here:
            # loud media pumping through the compressor (lower gain) and Wi-Fi timing
            # bursts (raise the buffer). No restart, no terminal, no lost session.
            b = self._body()
            with SESSION._lock:
                if b.get("auto_jitter") is True:
                    SESSION.return_jitter_manual = False
                    st.pop("return_manual_jitter_ms", None)
                    BRIDGEWATCH._tuning_stamp = None
                    save_state(st)
                elif b.get("jitter_ms") is not None:
                    try:
                        pinned = max(0, min(1000, int(b["jitter_ms"])))
                    except (TypeError, ValueError, OverflowError):
                        return self._send({"error": "jitter_ms must be an integer"}, 400)
                    SESSION.return_jitter_manual = True
                    st["return_manual_jitter_ms"] = pinned
                    save_state(st)
                result = SESSION.set_return_tuning(b.get("gain"), b.get("jitter_ms"), b.get("dynamics"), b.get("sink_sync"), b.get("conceal"), b.get("fec"), b.get("plc"), b.get("clock_mode"))
            return self._send({"ok": True, **result, "manual_jitter": SESSION.return_jitter_manual})

        if self.path == "/api/audio/capture":
            try:
                with SESSION._lock:
                    if not SESSION.return_proc or not hasattr(SESSION.return_proc, "start_capture"):
                        return self._send({"error": "Persistent return playback is required"}, 409)
                    result = SESSION.return_proc.start_capture(float(self._body().get("seconds", 15)))
                return self._send(result)
            except (ValueError, TypeError, RuntimeError) as exc:
                return self._send({"error": str(exc)}, 400)

        if self.path == "/api/audio/recover":
            with SESSION._lock:
                if not SESSION.wanted or not SESSION.return_on:
                    return self._send({"error": "Return playback is not enabled in a live session"}, 409)
                now = time.monotonic()
                if now - getattr(SESSION, "last_audio_recovery", -30) < 30:
                    return self._send({"error": "Wait 30 seconds between recovery attempts"}, 429)
                SESSION.last_audio_recovery = now
                SESSION.set_return(False)
                SESSION.set_return(True)
                result = SESSION.audio_snapshot()
                if not result.get("running"):
                    return self._send({**result, "error": "Return audio could not restart; inspect diagnostics"}, 503)
                return self._send(result)

        if self.path == "/api/microphone":
            if not isinstance(b.get("muted"), bool):
                return self._send({"error": "muted must be a boolean"}, 400)
            try:
                muted = SESSION.set_voice_muted(b["muted"])
                return self._send({"ok": True, "voice_muted": muted})
            except RuntimeError as exc:
                return self._send({"error": str(exc)}, 409)

        if self.path == "/api/return":
            # "Play meeting audio here" toggle — starts/stops the local return player only.
            body = self._body()
            on = SESSION.set_return(bool(body.get("on", True)))
            return self._send({"ok": True, "return_on": on, "return_player": SESSION.return_player})

        return self._send({"_error": "not found"}, 404)


UI = r"""<!doctype html><meta charset=utf8><title>NetBridge Source</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 :root{--pa:#F4F6F5;--cd:#fff;--ink:#1A2422;--mut:#5F6E68;--ln:#DCE3DF;--ok:#177A4C;
       --okb:#E4F1E9;--red:#C6392C;--redb:#F7E9E7;--fa:#8A968F}
 @media(prefers-color-scheme:dark){:root{--pa:#0E1412;--cd:#151D1A;--ink:#E8EFEA;
       --mut:#9AAAA2;--ln:#26312D;--ok:#5CC98C;--okb:#12271C;--red:#E8705F;--redb:#2E1A17;--fa:#75857D}}
 *{box-sizing:border-box} body{margin:0;background:var(--pa);color:var(--ink);
   font:15px/1.5 "Avenir Next","Segoe UI",system-ui,sans-serif}
 .w{max-width:520px;margin:0 auto;padding:26px 20px 60px}
 h1{font-size:20px;margin:0 0 2px;letter-spacing:-.02em}
 .sub{color:var(--mut);font-size:13px;margin:0 0 20px}
 .card{background:var(--cd);border:1px solid var(--ln);border-radius:12px;padding:16px;margin-bottom:14px}
 label{display:block;font:600 10.5px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
   text-transform:uppercase;color:var(--fa);margin-bottom:5px}
 select,input{width:100%;padding:9px 11px;border:1px solid var(--ln);border-radius:9px;
   background:var(--pa);color:var(--ink);font-size:14px;margin-bottom:11px}
 button{width:100%;padding:11px;border:0;border-radius:10px;background:var(--red);color:#fff;
   font-weight:700;font-size:14.5px;cursor:pointer}
 button.sec{background:var(--ink);color:var(--pa)}
 button:disabled{opacity:.45;cursor:not-allowed}
 .row{display:flex;justify-content:space-between;gap:10px;font-size:13.5px;padding:5px 0}
 .row .lat{font:11.5px ui-monospace,Menlo,monospace;color:var(--fa)}
 .ok{color:var(--ok);font-weight:700} .bad{color:var(--red);font-weight:700}
 .pill{display:inline-block;font-size:11px;font-weight:700;border-radius:999px;padding:2px 9px}
 .pill.on{background:var(--okb);color:var(--ok)} .pill.off{background:var(--redb);color:var(--red)}
 .msg{font-size:12.5px;color:var(--mut);margin-top:9px;min-height:17px}
 .who{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);margin-bottom:12px}
 .trow{display:flex;justify-content:space-between;align-items:center;font-size:13.5px;
   padding:9px 0 2px;margin-top:6px;border-top:1px solid var(--ln)}
 .sw{position:relative;display:inline-block;width:38px;height:22px;flex:0 0 auto}
 .sw input{opacity:0;width:0;height:0;position:absolute}
 .sw .sl{position:absolute;inset:0;background:var(--ln);border-radius:999px;transition:.15s;cursor:pointer}
 .sw .sl:before{content:"";position:absolute;height:16px;width:16px;left:3px;top:3px;
   background:#fff;border-radius:50%;transition:.15s}
 .sw input:checked + .sl{background:var(--ok)}
 .sw input:checked + .sl:before{transform:translateX(16px)}
 .hint{font-size:12px;color:var(--mut);margin:8px 0 0}
 .modal{position:fixed;inset:0;background:rgba(8,14,12,.5);display:flex;align-items:center;
   justify-content:center;padding:16px;z-index:20}
 .modal[hidden]{display:none}
 .sheet{background:var(--cd);border:1px solid var(--ln);border-radius:14px;padding:18px 18px 16px;
   width:100%;max-width:360px;box-shadow:0 18px 50px rgba(0,0,0,.28)}
 .sheet h2{font-size:17px;margin:0 0 3px;letter-spacing:-.01em}
 .sheet .sub{margin:0 0 14px}
 .sheet input{font-size:20px;letter-spacing:.3em;text-align:center}
 .pinmsg{font-size:12.5px;min-height:17px;margin:-3px 0 10px;color:var(--red)}
 .btns{display:flex;gap:10px}
 .btns button{flex:1}
</style>
<div class=w>
<h1>NetBridge Source</h1><p class=sub>Sign in, pick your bridge, go live with its PIN.</p>
<div class=who><span id=who>not signed in</span>
  <span><button id=signout onclick=signout() style="display:none;width:auto;padding:3px 10px;font-size:11.5px;background:var(--ink);color:var(--pa)">sign out</button>
  <span id=livepill></span></span></div>

<div class=card id=signin>
  <details><summary>Advanced connection settings</summary><label>Fleet address</label><input id=curl value="https://fleet.scine.online" placeholder="https://fleet.scine.online"></details>
  <label>Work email</label><input id=email placeholder="you@company.com">
  <button class=sec onclick=req()>Email me a sign-in code</button>
  <div style=height:11px></div>
  <label>Sign-in code</label><input id=code placeholder="paste the code">
  <button onclick=redeem()>Sign in</button>
  <div class=msg id=m1></div>
</div>

<div class=card id=main style=display:none>
  <label>Bridge</label><select id=bridge></select>
  <label>Camera</label><select id=cam></select>
  <label>Microphone</label><select id=mic></select>
  <button class=sec onclick=checkSetup()>Check my setup</button>
  <div id=setupReport role=status aria-live=polite></div>
  <details><summary>Get help</summary><p>Share a small status report with your fleet administrator. No recordings or raw logs.</p>
  <button class=sec onclick=previewSupport()>Preview support report</button><pre id=supportPreview></pre>
  <button class=sec id=sendSupport hidden onclick=sendSupport()>Send to fleet administrator</button><p id=supportResult role=status></p></details>
  <button id=go onclick=golive()>Go live</button>
  <p class=hint>You enter the bridge PIN every time you go live.</p>
  <div class=msg id=m2></div>
</div>

<div class=modal id=pinbox hidden>
 <div class=sheet role=dialog aria-modal=true aria-labelledby=pinTitle>
  <h2 id=pinTitle>Bridge PIN</h2>
  <p class=sub id=pinWhy>Every go-live needs the bridge's PIN.</p>
  <input id=pin type=password inputmode=numeric autocomplete=off maxlength=8 placeholder="4–8 digits"
    aria-describedby=pinMsg>
  <div class=pinmsg id=pinMsg role=alert></div>
  <div class=btns><button class=sec type=button onclick="pinSnooze=Date.now();pinClose()">Cancel</button>
   <button id=pinGo type=button onclick=pinSubmit()>Go live</button></div>
 </div>
</div>

<div class=card id=health style=display:none>
  <p id=meshPath class=lat></p>
  <div class=row><span id=c1>Bridge online</span><span class=lat id=l1></span></div>
  <div class=row><span id=c2>Your video arriving at bridge</span><span class=lat id=l2></span></div>
  <div class=row><span id=c5>Your voice arriving at bridge</span><span class=lat id=l5></span></div>
  <div class=row><span id=c3>USB connection configured</span><span class=lat id=l3></span></div>
  <div class=row><span id=c4>Meeting audio flowing back</span><span class=lat id=l4></span></div>
  <div id=ckfix style="display:none;margin-top:9px;padding:9px 11px;border-radius:8px;
    background:rgba(198,57,44,.09);border:1px solid rgba(198,57,44,.28);
    font-size:12.5px;line-height:1.5;color:var(--red)"></div>
  <div class=trow><span>Play meeting audio here</span>
    <label class=sw><input type=checkbox id=playhere checked onchange=togglePlay()><span class=sl></span></label></div>
</div>
<details id=audioDiagnostics class=card hidden style="margin-top:12px"><summary>Advanced audio diagnostics</summary>
<p id=audioSummary>Waiting for receiver…</p>
<button onclick="captureAudio()">Record 15-second diagnostic</button>
<button onclick="recoverAudio()">Restart return audio</button>
<p>Recording includes meeting audio and incoming packets. Saved on this laptop only.</p>
<p id=audioAction></p><a id=audioDownload hidden>Play captured audio</a>
<pre id=audioStats style="white-space:pre-wrap;max-height:260px;overflow:auto;font-size:11px"></pre>
</details>
<div style="text-align:center;font-size:11.5px;color:var(--faint);margin-top:10px">
  <span id=updnote></span> <span id=ver style="opacity:.6"></span></div>
</div>
<script>
const $=id=>document.getElementById(id); let BR=[],timer=null;
const j=(u,o)=>fetch(u,o).then(r=>r.json());
async function refreshAudio(){
 if($('audioDiagnostics').hidden||!$('audioDiagnostics').open)return;
 try{
  const a=await j('/api/audio/diagnostics');
  $('audioSummary').textContent=a.backend==='gstreamer-persistent'
   ? `${a.running?'Running':'Stopped'} · ${a.settings.jitter_ms} ms receive buffer · lost ${a.jitter['num-lost']||0} · late ${a.jitter['num-late']||0}`
   : (a.detail||'Persistent receiver not active');
  $('audioStats').textContent=JSON.stringify(a,null,2);
  if(a.capture){
   const c=a.capture;
   $('audioAction').textContent=c.active?'Recording…':c.valid?'Capture complete':'Capture incomplete: '+(c.error||'missing audio or dropped capture buffers');
   $('audioDownload').hidden=!c.valid;
   if(c.valid)$('audioDownload').href='/api/audio/captures/'+c.id+'/decoded.wav';
  }
 }catch(e){$('audioSummary').textContent='Diagnostics unavailable';}
}
async function captureAudio(){
 const r=await j('/api/audio/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seconds:15})});
 $('audioAction').textContent=r.error||'Recording started'; await refreshAudio();
}
async function recoverAudio(){
 const r=await j('/api/audio/recover',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 $('audioAction').textContent=r.error||'Return audio restarted'; await refreshAudio();
}
setInterval(refreshAudio,2000);refreshAudio();
function host(){const b=BR.find(x=>x.id===$('bridge').value);return b?(b.tailscale_ip||b.ip||''):''}
async function boot(){
  const s=await j('/api/state');
  if(s.control_url)$('curl').value=s.control_url;
  if(s.wanted)liveHost=s.live_host||(s.pin&&s.pin.host)||'';
  if(typeof s.return_on==='boolean')$('playhere').checked=s.return_on;
  if(s.signed_in){$('who').textContent='Signed in as '+(s.email||'');$('signout').style.display='';
    $('signin').style.display='none';$('main').style.display='';await load(s)}
  setLive(s.live||s.wanted);
  // Walkthrough J3: "Updated to 2.3.1 while you were away". Only rendered when there is
  // actually something to say — an update just applied, or one is staged for next start.
  if(s.update_note)$('updnote').textContent=s.update_note;
  $('ver').textContent='v'+(s.version||'?');
}
function setLive(v){
  for(const id of ['bridge','cam','mic'])$(id).disabled=!!v;
  $('audioDiagnostics').hidden=!v;
  $('livepill').innerHTML=v?'<span class="pill on">● LIVE</span>':'';
  $('go').textContent=v?'End session':'Go live';$('health').style.display=v?'':'none';
  if(v&&!timer)timer=setInterval(poll,4000); if(!v&&timer){clearInterval(timer);timer=null}}
async function togglePlay(){const on=$('playhere').checked;
  const r=await j('/api/return',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({on})});
  if(r&&typeof r.return_on==='boolean')$('playhere').checked=r.return_on}
async function signout(){await j('/api/signout',{method:'POST',headers:{'Content-Type':'application/json'}});location.reload()}
async function req(){const r=await j('/api/signin-request',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({control_url:$('curl').value,email:$('email').value})});
  $('m1').textContent=r._error||r.note||''}
async function redeem(){const r=await j('/api/signin-redeem',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('code').value,control_url:$('curl').value})});
  if(r._error){$('m1').textContent=r._error;return} location.reload()}
async function load(s){
  const b=await j('/api/bridges');
  if(b._error){
    // A 401 here means the signed-in account no longer exists or its token was revoked.
    // Showing an empty bridge list makes that look like "you have no bridges", which is
    // the wrong problem to go hunting for.
    if(b._code===401){
      $('m2').innerHTML='Your sign-in is no longer valid — <b>sign out and sign in again</b>.';
      $('signout').style.display='';
      return;
    }
    $('m2').textContent=b._error; return;
  }
  BR=b; $('bridge').innerHTML=b.map(x=>`<option value="${x.id}">${x.label?x.label+' · ':''}${x.name}`+
    `${x.label?'':' · '+x.pairing_code}${x.online?'':' (offline)'}</option>`).join('');
  if(s.live_bridge_id||s.last_bridge)$('bridge').value=s.live_bridge_id||s.last_bridge;
  const d=await j('/api/devices');
  $('cam').innerHTML=(d.video||[]).map(x=>`<option>${x.name}</option>`).join('');
  $('mic').innerHTML=(s.system_default_mic?[{name:'System default microphone'}]:(d.audio||[])).map(x=>`<option>${x.name}</option>`).join('');
  if(s.last_camera)$('cam').value=s.last_camera; if(s.last_mic)$('mic').value=s.last_mic;
  for(const el of ['bridge','cam','mic'])$(el).onchange=remember;
}
function remember(){fetch('/api/remember',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({bridge_id:$('bridge').value,camera_name:$('cam').value,mic_name:$('mic').value})})}
// ---- the PIN, every go-live ------------------------------------------------------------
// The PIN is typed here, sent once to this app (127.0.0.1 only), checked ON THE BRIDGE, and the
// field is cleared at once. What the bridge returns (a one-session ticket) stays inside the app
// process and never reaches this page. Stop ends the session on the bridge.
let liveHost='', pinSnooze=0, pinBlockedUntil=0, pinBlockedMessage='';
const FATAL_PIN=['no_pin','old_bridge','locked_out','locked_out_now'];
function openPin(why,err){
  $('pinWhy').textContent=why||"Every go-live needs the bridge's PIN.";
  $('pinMsg').textContent=err||''; $('pin').value=''; $('pinbox').hidden=false;
  setTimeout(()=>$('pin').focus(),0)}
function pinClose(){$('pinbox').hidden=true;$('pin').value='';$('pinMsg').textContent=''}
async function pinSubmit(){
  const pin=$('pin').value.trim(); $('pin').value='';
  if(!/^[0-9]{4,8}$/.test(pin)){$('pinMsg').textContent='The PIN is 4 to 8 digits.';$('pin').focus();return}
  $('pinGo').disabled=true; $('pinMsg').textContent='checking the PIN on the bridge…';
  try{ await startSession({pin}); } finally { $('pinGo').disabled=false; }}
$('pin').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();pinSubmit()}
  if(e.key==='Escape'){pinSnooze=Date.now();pinClose()}});
async function startSession(extra){
  const h=liveHost||host(); if(!h){pinClose();$('m2').textContent='bridge has no reachable address';return false}
  if(!(extra&&extra.pin))$('m2').textContent='reconnecting…';
  const r=await j('/api/golive',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({host:h,camera_name:$('cam').value,mic_name:$('mic').value},extra||{}))});
  if(r._error){
    if(FATAL_PIN.includes(r.reason)){
      pinBlockedUntil=Date.now()+Math.max(60,Number(r.retry_in)||3600)*1000;
      pinBlockedMessage=r._error;pinClose();checksUnavailable(r._error);$('m2').textContent=r._error;return false}
    if(r.reason==='wrong'||r.reason==='bad_format'){openPin($('pinWhy').textContent,r._error);return false}
    if(r.need_pin){openPin(r._error,'');return false}
    pinClose(); $('m2').textContent=r._error; return false}
  pinBlockedUntil=0;pinBlockedMessage='';pinClose();
  if(r.resumed){$('m2').textContent=r.note||'PIN accepted';$('ckfix').style.display='none';return true}
  liveHost=h;
  const warn = !['gstreamer','gstreamer-persistent','off'].includes(r.return_player);
  const via = r.via==='mesh' ? 'via secure mesh' : (r.via||'direct');
  $('m2').innerHTML=`live · ${r.camera} · ${r.mic} · <b>${via}</b><br>`+
    `<span style="color:${warn?'var(--red)':'var(--ok)'}">${r.return_note||''}</span>`;
  setLive(true); poll(); return true}
async function golive(){
  if($('go').textContent==='End session'){
    await j('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    setLive(false); liveHost='';
    $('m2').textContent='session ended — the bridge is locked again';return}
  if(!host()){$('m2').textContent='bridge has no reachable address';return}
  openPin()}
// Walkthrough J3 step 5: "If one goes red, the app says what to do in plain words."
// The device's `detail` is a MEASUREMENT ("usb gadget state: not attached") — true, but it
// tells a presenter mid-meeting nothing about what to DO. Each red check therefore carries
// its own remediation, written as an instruction, not a diagnosis. Green rows keep showing
// the measurement, which is the useful thing when everything is fine.
// One of these is only ever RIGHT when this app is actually sending. If the media legs have
// dropped, video and voice go red no matter how healthy the camera is — the packets are
// discarded on this Mac before they reach the network. Telling a presenter to close Zoom in
// that state sends them hunting the wrong thing; LEG_FIX below replaces the advice whenever
// the watchdog says the legs are gone.
const FIXES={
  online:'Bridge is not answering. Check it has power and its Wi-Fi is up, then try again.',
  video_arriving:'Your camera is not reaching the bridge. Close other apps using the camera (Zoom, Photo Booth), then End session and go live again.',
  voice_arriving:'Your mic is not reaching the bridge. Pick a different microphone above, then End session and go live again.',
  client_sees_camera:'The bridge has not confirmed a configured USB connection. Re-seat the USB cable at the laptop end, then pick "NetBridge" as the camera in Zoom/Teams.',
  return_audio:'No sound coming back. Play something on the meeting laptop and make sure its output is set to the NetBridge speaker.'};
// Older bridges judge return audio against a hardcoded ">40000 frames/s", which only 48kHz
// can ever reach — so a perfectly healthy 32kHz meeting (~34000/s) is reported red while the
// presenter hears clean audio. A check that cries wolf is worse than no check: the presenter
// stops believing the green lights. Re-judge here from the number the bridge already reports:
// if the measured pace sits within 4% of a rate the gadget actually offers, audio IS flowing.
// Newer bridges do this themselves and their verdict is already correct, so this only ever
// rescues a false red — it never turns a real failure green (a stalled stream reports a pace
// near zero, which matches no rate).
// The lowest rate the gadget offers. "Flowing" means the capture is moving at something
// like an audio rate - NOT that we can identify which one. Measured pace runs a few percent
// above nominal (the sampling window is wall-clock, not sample-clock), and 44.1k and 48k sit
// only 8.8% apart, so nearest-rate matching mislabels; a floor does not.
const MIN_OFFERED_RATE=32000;
// A rate mismatch self-heals in ~10s, so a REAL one persists across at least two polls.
// A single red sample is far more likely to be the measurement artifact: the bridge divides
// the hw_ptr delta by a hardcoded 2.0s while the numerator also spans two pgrep calls, so a
// slow pgrep inflates the computed rate past the 12% alarm threshold and the panel says
// "audio is pitch-shifted" about audio that is completely fine. Fixed properly on the device,
// but this guard means an older bridge stops crying wolf too.
let mismatchStreak = 0;
function rescueReturnAudio(v){
  if(!v||v.ok){ mismatchStreak = 0; return v; }
  const d=v.detail||'';
  if(/mismatch/i.test(d)){
    if(++mismatchStreak < 2)
      return {ok:true, detail:'checking rate…'};   // one sample is not evidence
    return v;                                      // persisted: a real mismatch, stay red
  }
  mismatchStreak = 0;
  const m=/~\s*(\d+)\s*\/s/.exec(d);
  if(!m)return v;                                 // "stream not open" has no pace: stays red
  const pace=parseInt(m[1],10);
  return pace>0.7*MIN_OFFERED_RATE
    ? {ok:true,detail:'audio flowing (~'+pace+' frames/s)'}
    : v;                                          // stalled stream reports near zero: stays red
}
// Auto-recovery is deliberately ONE attempt per outage, not a retry loop. Rebuilding the
// helper interrupts the session; doing it repeatedly against a fault that rebuilding cannot
// fix would leave a presenter in a permanent reconnect cycle mid-meeting, which is worse
// than one honest red line telling them what is wrong.
// ---- meeting-grade session state -------------------------------------------------
// Three things a presenter needs that raw checks do not give them:
//   * a bridge REBOOT is invisible: every local check still passes while the session is
//     actually dead, because our ffmpeg keeps sending to a device that no longer has the
//     matching state. Detected by uptime going backwards, then rebuilt automatically —
//     the session is already dead, so reconnecting cannot make it worse.
//   * green RIGHT NOW is not green RELIABLY. "Ready to present" waits for a continuous
//     clean run, so a flapping fault cannot be mistaken for a working setup.
//   * during a leg repair the meeting sees a brief FREEZE (the bridge pump re-sends its
//     cached frame), not the status card — so a calm one-line notice is honest.
let lastBridgeUptime=null, rebootRecovering=false;
const READY_AFTER_MS=30000;
let greenSince=null;

async function recoverFromReboot(){
  if(rebootRecovering)return; rebootRecovering=true;
  $('m2').textContent='Bridge restarted — reconnecting…';
  try{
    // Same session: keep this app's ticket. A reboot relocks the bridge, so the go-live that
    // follows gets "enter the PIN" back and the dialog opens with the reason.
    await j('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({keep_session:true})});
    setLive(false);
    await startSession({reconnect:true});
  } finally { rebootRecovering=false; lastBridgeUptime=null; }
}

let legRepairDone=false;
async function repairLegs(missing){
  if(legRepairDone)return; legRepairDone=true;
  // Transport legs repair independently in the mesh helper. Never stop a healthy
  // camera/microphone to fix a return-audio socket.
  $('m2').textContent='Media path unavailable';

}
async function previewSupport(){
 try{const r=await j('/api/support-report',{method:'POST',body:JSON.stringify({submit:false})});
 if(r._error)throw Error(r._error);$('supportPreview').textContent=r.notice+'\n'+JSON.stringify(r.report,null,2);$('sendSupport').hidden=false;
 }catch(e){$('supportResult').textContent=e.message;}
}
async function sendSupport(){
 $('sendSupport').disabled=true;
 try{const r=await j('/api/support-report',{method:'POST',body:JSON.stringify({submit:true,bridge_id:$('bridge').value})});
 if(r._error)throw Error(r._error);$('supportResult').textContent='Sent: '+r.reference;
 }catch(e){$('supportResult').textContent='Could not send. Copy the preview above for support. '+e.message;}
 finally{$('sendSupport').disabled=false;}
}
async function checkSetup(){
 const out=$('setupReport');out.textContent='Checking setup…';
 try{const r=await j('/api/preflight',{method:'POST',body:JSON.stringify({bridge_id:$('bridge').value,camera_name:$('cam').value,mic_name:$('mic').value})});
 if(r._error)throw Error(r._error);out.replaceChildren();
 for(const c of r.checks){const p=document.createElement('p');p.textContent=c.label+' — '+c.status+': '+c.detail;out.append(p);}}
 catch(e){out.textContent='Setup could not be checked: '+e.message;}
}
function checksUnavailable(message){
  greenSince=null;setCheckTone(false);$('meshPath').textContent='Connection path not verified';
  for(const [ci,li] of [['c1','l1'],['c2','l2'],['c5','l5'],['c3','l3'],['c4','l4']]){
    $(ci).className='bad'; $(li).textContent='Not currently verified';
  }
  $('ckfix').textContent=message; $('ckfix').style.display='';
  $('m2').textContent='Connection status unavailable — checking…';
}
function setCheckTone(ready){
  $('ckfix').style.color=ready?'var(--ok)':'var(--red)';
  $('ckfix').style.background=ready?'rgba(24,128,90,.08)':'rgba(198,57,44,.09)';
  $('ckfix').style.borderColor=ready?'var(--ok)':'rgba(198,57,44,.28)';
}
async function poll(){
  setCheckTone(false);
  const h=liveHost||host(); if(!h)return;
  let c;
  try{const state=await j('/api/state');
    if(state&&state.interruption&&!state.wanted){setLive(false);$('m2').textContent=state.interruption;return;}
    c=await j('/api/checks?host='+encodeURIComponent(h));}
  catch(e){checksUnavailable('Cannot reach the bridge checks. Your session has not been stopped.');return;}
  if(!c||c._error){checksUnavailable((c&&c._error)||'Bridge checks unavailable.');return;}
  // The bridge ended our PIN session (10 min without video, an admin lock, someone else's PIN).
  // It refuses our media now, so every red row below would send the presenter the wrong way.
  if(Date.now()<pinBlockedUntil){checksUnavailable(pinBlockedMessage);$('m2').textContent=pinBlockedMessage;return;}
  const pin=c.pin||{};
  if(pin.locked && (pin.protocol||1)>=2){
    if(typeof c._bridge_uptime_s==='number')lastBridgeUptime=c._bridge_uptime_s;
    const s=await j('/api/state');
    const why=(s.pin&&s.pin.lost_message)||'The bridge locked itself. Enter the PIN to continue.';
    checksUnavailable(why);
    $('m2').textContent='Bridge locked — enter the PIN to continue';
    if($('pinbox').hidden && Date.now()-pinSnooze>60000)openPin(why,'');   // not every 4 s after a Cancel
    return}
  const map=[['c1','l1','online'],['c2','l2','video_arriving'],['c5','l5','voice_arriving'],
             ['c3','l3','client_sees_camera'],['c4','l4','return_audio']];
  let firstBad=null;
  for(const [ci,li,k] of map){let v=c[k]||{};
    if(k==='return_audio')v=rescueReturnAudio(v);
    $(ci).className=v.ok?'ok':'bad';
    $(li).textContent=(v.detail||'').slice(0,42);
    if(!v.ok&&!firstBad)firstBad=k;}
  // The legs outrank every per-check fix. When they are down the red rows are a SYMPTOM,
  // and the camera/mic advice above is actively wrong — this app is not sending anywhere.
  const legs=c._legs||{ok:true};
  if(!legs.ok){
    const names={5000:'video',5002:'voice',5004:'return audio'};
    const lost=(legs.missing||[]).map(p=>names[p]||p).join(' and ');
    $('ckfix').textContent=lost+' path unavailable for '+(legs.down_for_s||0)+'s. Other working media remains active.';
    $('ckfix').style.display='';
    repairLegs(legs.missing||[]);
    return;
  }
  legRepairDone=false;   // healthy again: re-arm for the next outage

  // --- did the bridge reboot under us? uptime going BACKWARDS is unambiguous ---
  const up=c._bridge_uptime_s;
  if(typeof up==='number'){
    if(lastBridgeUptime!==null && up < lastBridgeUptime-30){
      lastBridgeUptime=up; greenSince=null;
      $('ckfix').textContent='The bridge restarted. Reconnecting your session…';
      $('ckfix').style.display='';
      recoverFromReboot();
      return;
    }
    lastBridgeUptime=up;
  }

  // --- readiness: green for a continuous stretch, not just this instant ---
  // Show the path whenever it is known. "direct" is quietly reassuring; "relay" is the
  // one you want to see BEFORE the meeting, because it roughly doubles the latency.
  const mp=c._mesh_path||{};
  if(mp.via==="relay") $('meshPath').textContent='⚠ relayed path ('+(mp.detail||'derp')+') — higher latency than direct';
  else if(mp.via==="direct") $('meshPath').textContent='Direct connection';
  else $('meshPath').textContent='Connection path not verified';

  const guard=c._guard||{};
  const repaired=Object.keys(guard.repairs||{}).length>0;
  if(firstBad){ greenSince=null; }
  else if(greenSince===null){ greenSince=Date.now(); }

  if(guard.waiting_for_mic){
    $('ckfix').textContent='No microphone is available. Audio resumes when the computer has an input device.';
    $('ckfix').style.display='';
  } else if(firstBad){
    // One instruction at a time — a wall of five red fixes is noise. The first broken link
    // in the chain is almost always the cause of the ones after it.
    $('ckfix').textContent=FIXES[firstBad]||'';
    $('ckfix').style.display='';
  } else if((guard.abandoned||[]).length){
    $('ckfix').textContent='The '+guard.abandoned.join(' and ')+' feed keeps failing. '
      +'End session and go live again to rebuild it.';
    $('ckfix').style.display='';
  } else if(Date.now()-greenSince < READY_AFTER_MS){
    const s=Math.ceil((READY_AFTER_MS-(Date.now()-greenSince))/1000);
    $('ckfix').textContent='Checking your connection… ready in '+s+'s';
    $('ckfix').style.display='';
  } else {
    setCheckTone(true);
    $('ckfix').textContent=repaired
      ? 'Ready to present · recovered automatically from a brief interruption'
      : 'Ready to present';
    $('ckfix').style.display='';
  }}
boot();
</script>
"""


def _already_running():
    """Is another copy of this app already serving on our port? A connect probe: no socket
    options, no TIME_WAIT false alarms, the same on macOS and Windows."""
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5):
            return True
    except OSError:
        return False


def main():
    # ANOTHER COPY FIRST. The start-up sweep below kills every netbridge-mesh helper and our
    # ffmpeg/GStreamer processes as "orphans" - but when a copy is already running they are
    # ITS processes, and killing them cut a live meeting's video, voice and room audio before
    # this copy then said "already running" and quit (found 2026-09-25, present since the
    # sweep was added). Ask before touching anything; if one is running, just show it.
    if _already_running():
        url = "http://%s:%d/" % (HOST, PORT)
        print("\nNetBridge is already running — opening it: %s" % url)
        print("  (To switch versions, press Stop, quit that copy normally, then launch this one.)\n")
        _open_browser(url)
        raise SystemExit(0)
    # Explicit operator latency choice survives reconnects and fleet auto-tuning.
    saved_jitter = load_state().get("return_manual_jitter_ms")
    if saved_jitter is not None:
        try:
            SESSION.return_jitter_ms = str(max(0, min(1000, int(saved_jitter))))
            SESSION.return_jitter_manual = True
        except (ValueError, TypeError, OverflowError):
            pass
    # FIRST: install anything staged by a previous run. This happens before any port is
    # bound or any device is opened, so the app replaces itself with nothing in flight —
    # and re-execs, meaning this function runs again as the new build.
    apply_staged_update()
    # Reap anything orphaned by a previous crash/hard-quit before we start — a leftover mesh
    # helper holding the media ports would make the first go-live time out, and a leftover
    # ffmpeg/gst would keep the camera on / keep playing the room.
    _kill_orphan_mesh()
    _kill_orphan_media()
    from power_observer import PowerObserver
    global _POWER_OBSERVER
    _POWER_OBSERVER = PowerObserver(suspend_capture, resume_capture)
    if not _POWER_OBSERVER.active:
        print("[power] " + str(_POWER_OBSERVER.error), flush=True)
    # Watch the media legs for the life of the process. It self-gates on SESSION.live, so it
    # costs three failed binds every 3s while idle and nothing at all in attention.
    threading.Thread(target=LEGS.run, daemon=True).start()
    # Per-leg repair every 5s. Restarts only what actually died; never the session.
    threading.Thread(target=GUARD.run, daemon=True).start()
    threading.Thread(target=BRIDGEWATCH.run, daemon=True).start()
    st = load_state()
    # Report a completed update once, then clear it so it does not stick forever.
    global _update_note
    if st.get("updated_to") == APP_VERSION:
        _update_note = "Updated to %s while you were away." % APP_VERSION
        st.pop("updated_to", None); st.pop("updated_from", None)
        save_state(st)
    # Check for the NEXT one in the background — never blocks startup, and only ever
    # stages a file (the swap is the next launch). Re-checked on a timer, not just at
    # startup: a presenter can leave this running for days, and a startup-only check
    # means an update is not even DISCOVERED until the app has been restarted twice.
    if st.get("control_url"):
        def _update_loop(url):
            while True:
                try:
                    check_for_update(url)
                except Exception:
                    pass
                time.sleep(UPDATE_CHECK_S)
        threading.Thread(target=_update_loop, args=(st["control_url"],),
                         daemon=True).start()
    if not st.get("control_url") and len(sys.argv) > 1:
        st["control_url"] = sys.argv[1].rstrip("/")
        save_state(st)
    # THREADED, deliberately. /api/checks proxies the bridge and blocks ~2s by design (it
    # samples real hw_ptr and CPU deltas). On a single-threaded server that one slow call
    # starves everything else: the "Play meeting audio here" toggle and the tuning endpoint
    # would simply hang until they timed out, which reads as "the app is broken" and made
    # live audio tuning impossible. Each request is independent here, so threading is safe.
    # "Address already in use" on a FIXED, known port has exactly one meaning: another copy
    # of this app is running. Letting that surface as a raw Python traceback told a presenter
    # nothing and looked like a crash — the same "we know exactly what is wrong and say
    # something useless" failure this app keeps making. Say the useful thing instead.
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):     # EADDRINUSE: macOS 48, Linux 98
            print("\nNetBridge is already running.")
            print("  Open %s" % ("http://%s:%d/" % (HOST, PORT)))
            print("  Or quit the other copy first, then relaunch.\n")
            raise SystemExit(0)                        # not an error: the app IS available
        raise
    srv.daemon_threads = True
    url = "http://%s:%d/" % (HOST, PORT)
    print("NetBridge Source  ->  %s" % url)
    print("(local only; ctrl-c to quit)")

    # ALWAYS release the camera/mic + stop the return player + remove the mesh node on ANY
    # teardown — Ctrl-C (SIGINT), a kill (SIGTERM), or the Terminal window closing (SIGHUP).
    # Before, only the SIGINT/finally path cleaned up, so closing the terminal or killing the
    # app left ffmpeg holding the camera (light stays on) and GStreamer still playing the room.
    import signal as _signal
    _cleaned = {"done": False}
    def _cleanup(signum=None, frame=None):
        if _cleaned["done"]:
            return
        _cleaned["done"] = True
        try:
            _end_bridge_session()   # the bridge relocks now, not 10 min from now
        except Exception:
            pass
        try:
            SESSION.stop()      # kill the video/voice ffmpeg legs + the return player
        finally:
            try:
                MESH.stop()     # remove the ephemeral mesh node
            finally:
                if signum is not None:
                    os._exit(0)
    _sigs = [getattr(_signal, s) for s in ("SIGTERM", "SIGHUP", "SIGQUIT")
             if hasattr(_signal, s)]
    for _s in _sigs:
        try:
            _signal.signal(_s, _cleanup)
        except Exception:
            pass

    _open_browser(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()
        print("\nstopped.")


if __name__ == "__main__":
    main()
