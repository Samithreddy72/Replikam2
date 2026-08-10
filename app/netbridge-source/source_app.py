#!/usr/bin/env python3
"""NetBridge Source — the presenter app (walkthrough Journey 3).

Runs entirely on the presenter's machine and serves its UI on 127.0.0.1 only. It is the
thing that turns J3's six steps into software:

  1 install                 -> this app (packaging/signing is M7, needs an Apple account)
  2 sign in with work email -> magic link against the control plane, personal token
  3 pick bridge/camera/mic  -> real dropdowns, remembered BY NAME (ledger E4), never by
                               device index: avfoundation renumbers when you plug in a
                               headset, so a saved index silently streams the wrong camera
  4 unlock with the PIN     -> verified ON THE DEVICE (/api/unlock). Seeing a bridge in the
                               list is not permission to stream to it
  5 go live, four checks    -> spawns the same ffmpeg legs as mac-stream.sh and polls the
                               device's /api/checks, which measures real activity
  6 hear the room back      -> registers THIS machine as the return-audio peer through the
                               control plane (never SSH) and plays the return stream

Deliberately NOT in this app: any admin capability. It signs in as a viewer, so the
control plane refuses fleet mutations even if the UI asked for them.
"""
import json, os, pathlib, re, shutil, socket, subprocess, sys, threading, time
import urllib.request, urllib.error

STATE_DIR = pathlib.Path(os.path.expanduser("~/.netbridge-source"))
STATE_FILE = STATE_DIR / "state.json"
HOST, PORT = "127.0.0.1", 8765

# Ports the bridge listens on (bridge-feeder-net / bridge-feeder-audio).
RTP_VIDEO, RTP_VOICE = 5000, 5002

# Build stamp. build.py rewrites this line, and it is what the updater compares against
# the signed manifest — so a build that forgets to bump it simply never updates, rather
# than update-looping.
APP_VERSION = "1.1.3"


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
    """The file that gets replaced — only meaningful for a frozen (packaged) build."""
    return pathlib.Path(sys.executable) if getattr(sys, "frozen", False) else None


def _update_pubkey():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "app-pubkey.pem"),
              os.path.join(os.path.dirname(str(_app_binary() or "")), "app-pubkey.pem")):
        if p and os.path.exists(p):
            return p
    return None


def _verify_sig(pubkey, sig_path, data_path):
    """EC/SHA256 verify, same invocation the bridge uses. Absent openssl => unverifiable
    => refuse (never 'update anyway')."""
    if not shutil.which("openssl"):
        return False
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pubkey,
                        "-signature", sig_path, data_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_staged_update():
    """Swap in a previously staged update. Runs FIRST at startup, before any port is
    bound, so the app can replace itself with nothing in flight. Keeps the outgoing
    binary as .old — if the new one cannot start, that file is the way back."""
    global _update_note
    exe = _app_binary()
    if not exe:
        return
    staged, meta = exe.with_suffix(".new"), exe.with_suffix(".new.json")
    if not (staged.exists() and meta.exists()):
        return
    try:
        info = json.loads(meta.read_text())
        # Re-verify at apply time: the file sat on disk since the download.
        if _sha256(str(staged)) != info.get("sha256"):
            staged.unlink(missing_ok=True); meta.unlink(missing_ok=True)
            return
        old = exe.with_suffix(".old")
        old.unlink(missing_ok=True)
        os.replace(str(exe), str(old))       # atomic; the running image stays mapped
        os.replace(str(staged), str(exe))
        os.chmod(str(exe), 0o755)
        if not IS_WIN:
            # Ad-hoc re-sign: macOS SIGKILLs a binary whose signature does not match
            # its contents, which would brick the app on Apple Silicon.
            subprocess.run(["codesign", "--force", "--sign", "-", str(exe)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        meta.unlink(missing_ok=True)
        save_state({**load_state(), "updated_to": info.get("version"),
                    "updated_from": APP_VERSION})
        os.execv(str(exe), [str(exe)] + sys.argv[1:])     # start the new build
    except Exception:
        # Never let a failed swap stop the app from running the version it already has.
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
    root = "%s/app/%s" % (base_url.rstrip("/"), _plat_tag())
    tmp = pathlib.Path(STATE_DIR) / "update"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        man, sig = tmp / UPDATE_MANIFEST, tmp / (UPDATE_MANIFEST + ".sig")
        urllib.request.urlretrieve("%s/%s" % (root, UPDATE_MANIFEST), man)
        urllib.request.urlretrieve("%s/%s.sig" % (root, UPDATE_MANIFEST), sig)
        if not _verify_sig(pub, str(sig), str(man)):
            return None                  # unsigned/tampered manifest: stop here
        fields = dict(l.split("=", 1) for l in man.read_text().splitlines()
                      if "=" in l and not l.startswith("#"))
        ver, want, fname = fields.get("version"), fields.get("sha256"), fields.get("file")
        if not (ver and want and fname) or ver == APP_VERSION:
            return None
        blob = tmp / fname
        urllib.request.urlretrieve("%s/%s" % (root, fname), blob)
        if _sha256(str(blob)) != want:
            return None                  # signed manifest, wrong bytes: refuse
        staged = exe.with_suffix(".new")
        shutil.move(str(blob), str(staged))
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
            return {"_error": json.loads(e.read().decode()).get("detail", str(e)), "_code": e.code}
        except Exception:
            return {"_error": str(e), "_code": e.code}
    except Exception as e:
        return {"_error": str(e)}


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


def _is_exe(path):
    """True if path is a runnable binary. os.access(X_OK) is meaningless on Windows -
    a perfectly good bundled .exe can test False there, which would silently drop us back
    to a system ffmpeg that a presenter does not have. On Windows, existing is enough."""
    if not os.path.isfile(path):
        return False
    return True if IS_WIN else os.access(path, os.X_OK)


def _ffmpeg():
    """Prefer an ffmpeg shipped next to this app, fall back to one on PATH.

    A packaged build bundles its own binary so a presenter installs nothing (walkthrough
    J3 step 1: "the app brings everything"). Running from source, the system one is fine.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(base, "ffmpeg.exe" if IS_WIN else "ffmpeg")
    if _is_exe(local):
        return local
    return shutil.which("ffmpeg") or "ffmpeg"


def _gst():
    """Path to gst-launch-1.0 — the bundled copy if we shipped one, else the system's."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
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
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
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
def av_devices():
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
        p = subprocess.run(args, capture_output=True, text=True, timeout=25)
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
class Session:
    """Owns the live ffmpeg legs + the return listener."""

    def __init__(self):
        self.procs = []
        self.logs = []
        self.bridge = None
        self.return_port = 5004
        self.return_player = "none"
        self.voice_proc = None
        self.return_proc = None     # the return-audio player, tracked so it can be toggled
        self.return_on = True
        # Return-audio tunables, seeded from env and changeable mid-session (see
        # set_return_tuning). Kept on the session, not read from os.environ at each start,
        # so a presenter can correct pumping or jitter without quitting a live meeting.
        self.return_gain = os.environ.get("NB_RETURN_GAIN", "2.0")
        self.return_jitter_ms = os.environ.get("NB_RETURN_JITTER_MS", "250")
        # Compressor+limiter chain. On by default (it is what keeps voices audible over
        # loud rooms), but switchable: the dynamics stage is the prime suspect whenever the
        # artifact appears only on loud material, and bypassing it is the decisive test.
        self.return_dynamics = os.environ.get("NB_RETURN_DYNAMICS", "1") != "0"
        # Sink clock mode. sync=false was adopted as an anti-click fix (the sink's default
        # drift correction was skipping samples audibly) — but that was tuned when the return
        # path was direct. With sync=false the sink plays buffers as fast as they arrive while
        # the audio device consumes at its own crystal rate; the two clocks are never exactly
        # equal, so the ring buffer slowly drifts and periodically jumps. GStreamer documents
        # this as "periodic fast-forwarding every few seconds" — which is what a listener
        # calls jitter. Switchable so the two modes can be judged by ear on the real path.
        self.return_sink_sync = os.environ.get("NB_RETURN_SINK_SYNC", "0") != "0"
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

    @property
    def live(self):
        return any(p.poll() is None for p in self.procs)

    def voice_sending(self):
        """True if the mic->bridge leg is alive. A sender-side signal used only as a
        fallback when the bridge firmware predates the device-side voice_arriving check."""
        return bool(self.voice_proc and self.voice_proc.poll() is None)

    def start(self, pi_host, video_idx, audio_idx, fps=20, mic_gain=8, return_port=5004):
        self.stop()
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
            venc = ["-c:v", "h264_videotoolbox", "-realtime", "1"]
            ain = ["-f", "avfoundation", "-i", ":%s" % audio_idx]

        v = common + vin + [
            "-vf", "scale=320:180,format=nv12", "-fps_mode", "cfr", "-r", str(fps),
        ] + venc + [
            "-b:v", "400k", "-g", str(fps), "-bsf:v", "dump_extra=freq=keyframe", "-an",
            "-f", "rtp", "rtp://%s:%d?pkt_size=1100" % (pi_host, RTP_VIDEO)]
        a = common + ain + [
            "-af", "volume=%ddB,alimiter=limit=0.9" % mic_gain,
            "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "2",
            "-application", "lowdelay", "-payload_type", "97",
            "-f", "rtp", "rtp://%s:%d" % (pi_host, RTP_VOICE)]

        # Keep each leg's stderr so a dead leg can be explained instead of guessed at.
        self.logs = []
        self.voice_proc = None
        logdir = _logdir()
        for name, argv in (("video", v), ("voice", a)):
            lf = open(os.path.join(str(logdir), "netbridge-source-%s.log" % name), "w")
            self.logs.append(lf)
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=lf)
            self.procs.append(proc)
            if name == "voice":
                self.voice_proc = proc          # so the app can report the voice leg is alive

        self.return_player = self._start_return(return_port) if self.return_on else "off"

    def set_return_tuning(self, gain=None, jitter_ms=None, dynamics=None, sink_sync=None, conceal=None):
        """Change return-audio gain / buffer depth WITHOUT ending the session.

        These were env-vars read once at app launch, so trying a different value meant
        quitting mid-meeting - which is exactly when you discover you need one. The two
        documented failure modes both live here: loud media "pumping" through the
        compressor (lower the gain) and Wi-Fi timing bursts (raise the buffer). A presenter
        should be able to fix what they are hearing while they are hearing it."""
        if gain is not None:
            try: self.return_gain = "%.2f" % max(0.2, min(4.0, float(gain)))
            except (TypeError, ValueError): pass
        if jitter_ms is not None:
            try: self.return_jitter_ms = str(int(max(60, min(1000, int(jitter_ms)))))
            except (TypeError, ValueError): pass
        if dynamics is not None:
            self.return_dynamics = bool(dynamics)
        if sink_sync is not None:
            self.return_sink_sync = bool(sink_sync)
        if conceal is not None:
            self.return_conceal = bool(conceal)
        if self.return_on:            # re-open the player so the new values take effect
            self.set_return(False)
            self.set_return(True)
        return {"gain": self.return_gain, "jitter_ms": self.return_jitter_ms,
                "dynamics": self.return_dynamics, "sink_sync": self.return_sink_sync,
                "conceal": self.return_conceal, "player": self.return_player}

    def set_return(self, on):
        """Toggle 'Play meeting audio here' live, without disturbing the video/voice legs.
        Off stops just the local return player (the bridge keeps sending; you simply don't
        play it here — e.g. when you're listening on the meeting device itself)."""
        self.return_on = bool(on)
        running = self.return_proc and self.return_proc.poll() is None
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
        caps = ("application/x-rtp,media=audio,encoding-name=OPUS,"
                "payload=97,clock-rate=48000")
        gst = _gst()
        if gst:
            try:
                # Return-audio pipeline ported from the FIELD-PROVEN mac-return-listen.sh
                # (docs/AUDIO-TUNING.md, "0 late / 0 lost / 0 dropouts"). The app had drifted
                # from that recipe, and every deviation added the warble Samith heard:
                #   • opusdec plc=true + inband-fec + do-lost SYNTHESIZE audio to conceal loss —
                #     "tested cleaner" WITHOUT it (PLC's guesses are the artifacts). Use plain opusdec.
                #   • audioresample quality=10 = SoX-grade, transparent to the 48k output.
                #   • gentle soft-knee voice compressor + brick-wall hard-knee limiter (voices
                #     clear & boosted, loud media capped, never clips).
                #   • a decoupling queue so the sink never back-pressures the decoder.
                #   • osxaudiosink sync=false buffer-time/latency-time = THE anti-click sink: its
                #     DEFAULT drift-correction skips samples ~100x/s (audible clicking); sync=false
                #     plays the jitterbuffer's already-paced stream untouched.
                # This only works now because return audio is LAN-DIRECT — the nb-mesh relay that
                # previously masked the fix is out of the return path. Knobs via env for tuning
                # without a rebuild: NB_RETURN_JITTER_MS (buffer depth), NB_RETURN_GAIN (loudness).
                # Live values (seeded from env at startup, changeable mid-session via
                # /api/return-tuning) — see set_return_tuning for why.
                lat = getattr(self, "return_jitter_ms", None) or os.environ.get("NB_RETURN_JITTER_MS", "250")
                gain = getattr(self, "return_gain", None) or os.environ.get("NB_RETURN_GAIN", "2.0")
                chain = [gst, "-q",
                    "udpsrc", "port=%d" % port, "caps=" + caps, "!",
                    "rtpjitterbuffer", "latency=" + lat] + (
                        ["do-lost=true"] if getattr(self, "return_conceal", True) else []) + ["!",
                    "rtpopusdepay", "!",
                    "opusdec"] + (["use-inband-fec=true", "plc=true"]
                                  if getattr(self, "return_conceal", True) else []) + ["!",
                    "audioconvert", "!", "audioresample", "quality=10", "!",
                ]
                if getattr(self, "return_dynamics", True):
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
                sync = "true" if getattr(self, "return_sink_sync", False) else "false"
                if IS_MAC:
                    chain += ["osxaudiosink", "sync=" + sync,
                              "buffer-time=200000", "latency-time=20000"]
                else:
                    chain += ["autoaudiosink", "sync=" + sync]
                p = subprocess.Popen(chain, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=_gst_env())
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

    def stop(self):
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        deadline = time.time() + 4
        for p in self.procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.procs = []
        self.return_proc = None


SESSION = Session()


def _kill_orphan_media():
    """Startup-only reaper for OUR leftover ffmpeg/GStreamer from a hard-killed prior run.
    Normal exits are handled by the signal cleanup in main(); a SIGKILL/crash can't run any
    handler, so a fresh launch sweeps up its own zombies here. Matched by unmistakable arg
    signatures unique to us (our RTP video flag, our voice payload, our return udpsrc port),
    so no unrelated ffmpeg/gst on the machine is ever touched. Safe only at startup, before
    this instance has a live session of its own."""
    if IS_WIN:
        return  # rely on the signal cleanup; a broad image kill would be unsafe on Windows
    import signal as _sig
    for pat in ("dump_extra=freq=keyframe", "payload_type 97", "udpsrc port=5004"):
        try:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            for tok in out.stdout.split():
                try:
                    os.kill(int(tok), _sig.SIGKILL)
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
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
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


def _kill_orphan_mesh(exclude_pid=None):
    """Reap leftover netbridge-mesh helpers from a previous/crashed session.

    An orphaned helper keeps holding the media + control ports (5000/5002/5004/18080).
    The next go-live's helper then can't bind them and the session half-fails — the
    classic 'unlock timed out' with the video/voice checks stuck red. We own at most one
    helper (self.proc) which callers stop() first, so killing every other netbridge-mesh
    here is safe. Best-effort: never let cleanup raise into the go-live path."""
    import signal as _sig
    name = "netbridge-mesh.exe" if IS_WIN else "netbridge-mesh"
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            out = subprocess.run(["pgrep", "-f", "netbridge-mesh"],
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
        self.bridge_lan_ip = None   # the bridge's LAN IP, if it's on our network

    def _mint_key(self, st):
        r = api("POST", st["control_url"].rstrip("/") + "/auth/mesh-key",
                token=st.get("token"), timeout=25)
        if isinstance(r, dict) and not r.get("_error"):
            # the endpoint returns the key as "authkey" (not "key")
            return r.get("authkey"), r.get("login_server") or ""
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
            ip = (rec or {}).get("ip")
            return {"via": "direct", "control_host": ip, "control_port": 8080,
                    "media_host": ip, "return_peer": local_ip_towards(ip) if ip else ""}

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
            ip = rec.get("ip")
            return {"via": "direct", "control_host": ip, "control_port": 8080,
                    "media_host": ip, "return_peer": local_ip_towards(ip) if ip else ""}

        argv = [mesh_bin, "--authkey", key, "--bridge", tsip,
                "--hostname", "nb-source-%s" % (rec.get("id") or "app")[-6:],
                "--forward", "%d,%d" % (RTP_VIDEO, RTP_VOICE), "--return", "5004",
                "--control", "%d:8080" % self.CTRL_LOCAL]
        if login:
            argv += ["--login-server", login]
        lf = open(os.path.join(str(_logdir()), "netbridge-source-mesh.log"), "w")
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=lf, text=True)
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
            return {"via": "direct-fallback", "error": hs.get("error", "mesh did not start"),
                    "control_host": ip, "control_port": 8080, "media_host": ip,
                    "return_peer": local_ip_towards(ip) if ip else ""}
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
        if not SESSION.live or not MESH.proc or MESH.proc.poll() is not None:
            with self.lock:
                self.missing, self.strikes, self.live_since = [], 0, 0.0
            return
        with self.lock:
            if not self.live_since:
                self.live_since = time.time()          # session just came up
            young = (time.time() - self.live_since) < self.GRACE_S
        if young:
            return                                     # still binding; not our business yet
        gone = [p for p in (RTP_VIDEO, RTP_VOICE, SESSION.return_port) if not _leg_bound(p)]
        with self.lock:
            if gone:
                self.strikes += 1
                if self.strikes >= self.STRIKES and not self.missing:
                    self.missing = gone
                    self.since = time.time()
                    self.drops += 1
                    print("[legs] lost: %s — helper alive but not listening"
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


def _bridge_rec(host, st):
    """Find the bridge record for a UI-supplied host (its tailnet or LAN IP). Falls back
    to a bare {ip:host} so a hand-typed address still works."""
    lst = _BRIDGES["list"]
    if not lst and st.get("token") and st.get("control_url"):
        r = api("GET", st["control_url"].rstrip("/") + "/admin/devices", token=st.get("token"))
        if isinstance(r, list):
            lst = _BRIDGES["list"] = r
    for d in lst:
        if host in (d.get("tailscale_ip"), (d.get("latest") or {}).get("ip"), d.get("ip")):
            return {"id": d.get("id"), "tailscale_ip": d.get("tailscale_ip"),
                    "ip": (d.get("latest") or {}).get("ip") or d.get("ip")}
    return {"id": None, "tailscale_ip": None, "ip": host}


def bridge_route(host, st):
    """Base control URL + routing for a bridge, bringing the mesh up if appropriate."""
    rec = _bridge_rec(host, st)
    route = MESH.route(rec, st)
    route["base"] = "http://%s:%d" % (route["control_host"], route["control_port"])
    return route


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

    # ---------------- GET
    def do_GET(self):
        self._body_cache = None
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
                "last_mic": st.get("mic_name"),
                "live": SESSION.live,
                "legs": LEGS.snapshot(),
                "return_on": SESSION.return_on, "return_gain": SESSION.return_gain, "return_jitter_ms": SESSION.return_jitter_ms, "return_sink_sync": SESSION.return_sink_sync, "return_conceal": SESSION.return_conceal,
                "version": APP_VERSION,
                "update_note": _update_note,
            })
        if self.path == "/api/devices":
            return self._send(av_devices())
        if self.path == "/api/bridges":
            if not st.get("token"):
                return self._send({"_error": "not signed in"}, 401)
            r = api("GET", st["control_url"].rstrip("/") + "/admin/devices", token=st["token"])
            if isinstance(r, list):
                _BRIDGES["list"] = r     # cache for mesh routing (tailscale_ip per bridge)
            if isinstance(r, dict) and r.get("_error"):
                return self._send(r, 502)
            # presenters see their org's bridges; never any secret
            out = [{"id": d.get("id"), "name": d.get("name") or d.get("pairing_code"),
                    "pairing_code": d.get("pairing_code"), "online": d.get("online"),
                    "ip": (d.get("latest") or {}).get("ip"),
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
            host = self.path.split("host=", 1)[1] if "host=" in self.path else ""
            if not host:
                return self._send({"_error": "host required"}, 400)
            route = bridge_route(host, load_state())   # over the mesh when the bridge has one
            r = api("GET", route["base"] + "/api/checks", timeout=10)
            # Attach the leg state to the SAME response the UI already polls, so the front
            # end can tell "your camera is busy" apart from "this app is not sending
            # anywhere" without a second round trip.
            if isinstance(r, dict):
                r["_legs"] = LEGS.snapshot()
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

    # ---------------- POST
    def do_POST(self):
        self._body_cache = None      # fresh per request (connections are reused)
        st, b = load_state(), self._body()

        if self.path == "/api/signin-request":
            url = (b.get("control_url") or "").rstrip("/")
            email = (b.get("email") or "").strip()
            if not url or not email:
                return self._send({"_error": "control_url and email required"}, 400)
            st["control_url"] = url
            save_state(st)
            r = api("POST", url + "/auth/magic-link", body={"email": email})
            # The endpoint answers identically whether or not the user exists, so this
            # app must not imply the address was recognised.
            return self._send({"ok": True, "note": "if that address has an account, a "
                                                   "sign-in code is on its way", "raw": r})

        if self.path == "/api/signin-redeem":
            url = st.get("control_url", "").rstrip("/")
            code = (b.get("code") or "").strip()
            # Two ways in, and the presenter should not have to know which they were given:
            # a one-time INVITE (their very first sign-in, issued when an admin adds them)
            # or a magic-link CODE (every sign-in after that). Try the magic link first,
            # then fall back to the invite, so one box accepts either.
            r = api("POST", url + "/auth/magic-redeem", body={"code": code})
            if not (r or {}).get("token"):
                r = api("POST", url + "/auth/redeem", body={"invite": code})
            tok = (r or {}).get("token")
            if not tok:
                return self._send({"_error": (r or {}).get("_error") or "invalid or expired code"}, 401)
            st["token"] = tok
            # /auth/redeem already tells us who we are; whoami is a fallback so the header
            # never shows a blank identity after a successful sign-in.
            who = api("GET", url + "/auth/whoami", token=tok)
            st["email"] = (who or {}).get("email") or (r or {}).get("email")
            save_state(st)
            return self._send({"ok": True, "email": st["email"]})

        if self.path == "/api/signout":
            # Drop the identity, keep control_url and the remembered devices: a forced
            # sign-out should not make the presenter re-pick their camera and mic.
            for k in ("token", "email"):
                st.pop(k, None)
            save_state(st)
            MESH.stop()
            return self._send({"ok": True})

        if self.path == "/api/remember":
            for k in ("bridge_id", "camera_name", "mic_name"):
                if b.get(k) is not None:
                    st[k] = b[k]
            save_state(st)
            return self._send({"ok": True})

        if self.path == "/api/unlock":
            host, pin = b.get("host"), str(b.get("pin") or "")
            if not host or not pin:
                return self._send({"_error": "host and pin required"}, 400)
            # Verified ON THE DEVICE, reached over the mesh. The control plane is not asked
            # and cannot override it.
            #
            # SELF-HEAL. "unlock timed out" was by far the most common way a presenter got
            # stuck, and it was almost never a real failure: the mesh helper is usually just
            # YOUNGER THAN THE PATH IT NEEDS. If the helper joins while the bridge is still
            # booting (or briefly offline), tsnet has no route to that peer yet and the first
            # dial hangs — then the route comes up seconds later. The app used to make one
            # 20 s attempt and give up, so a presenter saw "timed out" against a bridge that
            # was fine and would have answered on the very next try.
            #
            # So: retry, and on the first failure rebuild the helper (that covers the other
            # case — a helper left over from a previous session that will never route). A
            # real answer from the device (wrong PIN / lockout) returns immediately; only
            # transport failures are retried, so a wrong PIN can never burn 3 of the 3
            # attempts the device allows before locking out.
            last = None
            for attempt in range(3):
                route = bridge_route(host, st)
                last = api("POST", route["base"] + "/api/unlock",
                           body={"pin": pin}, timeout=12)
                if not last.get("_error"):
                    return self._send(last)                 # unlocked, or a real PIN verdict
                if attempt == 0:
                    MESH.stop()                             # rebuild a helper that cannot route
                    _kill_orphan_mesh()
                time.sleep(2)
            # Still failing: say what is actually wrong instead of "timed out".
            reachable = _bridge_reachable(host, st)
            detail = ("the bridge is not answering — check it has power and is online"
                      if not reachable else
                      "the bridge is up but not reachable over the mesh yet; it may still be "
                      "starting up — wait a few seconds and try again")
            return self._send({"_error": detail, "attempts": 3,
                               "raw": last.get("_error") if last else None}, 502)

        if self.path == "/api/golive":
            host = b.get("host")
            if not host:
                return self._send({"_error": "host required"}, 400)
            devs = av_devices()
            vidx, vname, _ = resolve_by_name(devs.get("video", []), b.get("camera_name"))
            aidx, aname, _ = resolve_by_name(devs.get("audio", []), b.get("mic_name"), "0")
            port = int(b.get("return_port") or 5004)
            # Route over the mesh when the bridge has a tailnet address. Media then targets
            # 127.0.0.1 (the helper's local proxies) and the bridge is told to return audio
            # to OUR mesh IP - so no 100.x address is ever handled by the app itself.
            route = bridge_route(host, st)
            me = route["return_peer"]
            peer = api("POST", route["base"] + "/api/set-peer",
                       body={"ip": me, "port": port}, timeout=15) if me else {"_error": "no route"}
            SESSION.start(route["media_host"], vidx, aidx, return_port=port)
            st.update({"bridge_host": host, "camera_name": vname, "mic_name": aname})
            save_state(st)
            player = SESSION.return_player
            return self._send({"ok": True, "camera": vname, "mic": aname,
                               "return_peer": me, "return_port": port, "peer_result": peer,
                               "via": route.get("via"), "return_player": player,
                               "return_note": {
                                   "gstreamer": "room audio: GStreamer (best — jitter-buffered)",
                                   "ffmpeg": "room audio: ffmpeg fallback — install GStreamer for smoother playback",
                                   "none": "NO ROOM AUDIO — neither GStreamer nor ffmpeg could start a player",
                               }.get(player, player)})

        if self.path == "/api/stop":
            SESSION.stop()
            MESH.stop()
            return self._send({"ok": True})

        if self.path == "/api/return-tuning":
            # Live return-audio tuning. Both documented artifacts are fixed from here:
            # loud media pumping through the compressor (lower gain) and Wi-Fi timing
            # bursts (raise the buffer). No restart, no terminal, no lost session.
            b = self._body()
            return self._send({"ok": True,
                               **SESSION.set_return_tuning(b.get("gain"), b.get("jitter_ms"), b.get("dynamics"), b.get("sink_sync"), b.get("conceal"))})

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
</style>
<div class=w>
<h1>NetBridge Source</h1><p class=sub>Sign in, unlock your bridge, go live.</p>
<div class=who><span id=who>not signed in</span>
  <span><button id=signout onclick=signout() style="display:none;width:auto;padding:3px 10px;font-size:11.5px;background:var(--ink);color:var(--pa)">sign out</button>
  <span id=livepill></span></span></div>

<div class=card id=signin>
  <label>Control plane URL</label><input id=curl placeholder="https://fleet.scine.online">
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
  <label>Bridge PIN</label><input id=pin placeholder="6-digit PIN from your admin" inputmode=numeric>
  <button class=sec onclick=unlock()>Unlock bridge</button>
  <div style=height:11px></div>
  <button id=go onclick=golive()>Go live</button>
  <div class=msg id=m2></div>
</div>

<div class=card id=health style=display:none>
  <div class=row><span id=c1>Bridge online</span><span class=lat id=l1></span></div>
  <div class=row><span id=c2>Your video arriving at bridge</span><span class=lat id=l2></span></div>
  <div class=row><span id=c5>Your voice arriving at bridge</span><span class=lat id=l5></span></div>
  <div class=row><span id=c3>Meeting laptop sees the camera</span><span class=lat id=l3></span></div>
  <div class=row><span id=c4>Meeting audio flowing back</span><span class=lat id=l4></span></div>
  <div id=ckfix style="display:none;margin-top:9px;padding:9px 11px;border-radius:8px;
    background:rgba(198,57,44,.09);border:1px solid rgba(198,57,44,.28);
    font-size:12.5px;line-height:1.5;color:var(--red)"></div>
  <div class=trow><span>Play meeting audio here</span>
    <label class=sw><input type=checkbox id=playhere checked onchange=togglePlay()><span class=sl></span></label></div>
</div>
<div style="text-align:center;font-size:11.5px;color:var(--faint);margin-top:10px">
  <span id=updnote></span> <span id=ver style="opacity:.6"></span></div>
</div>
<script>
const $=id=>document.getElementById(id); let BR=[],timer=null;
const j=(u,o)=>fetch(u,o).then(r=>r.json());
function host(){const b=BR.find(x=>x.id===$('bridge').value);return b?(b.tailscale_ip||b.ip||''):''}
async function boot(){
  const s=await j('/api/state');
  if(s.control_url)$('curl').value=s.control_url;
  if(typeof s.return_on==='boolean')$('playhere').checked=s.return_on;
  if(s.signed_in){$('who').textContent='Signed in as '+(s.email||'');$('signout').style.display='';
    $('signin').style.display='none';$('main').style.display='';await load(s)}
  setLive(s.live);
  // Walkthrough J3: "Updated to 2.3.1 while you were away". Only rendered when there is
  // actually something to say — an update just applied, or one is staged for next start.
  if(s.update_note)$('updnote').textContent=s.update_note;
  $('ver').textContent='v'+(s.version||'?');
}
function setLive(v){$('livepill').innerHTML=v?'<span class="pill on">● LIVE</span>':'';
  $('go').textContent=v?'End session':'Go live';$('health').style.display=v?'':'none';
  if(v&&!timer)timer=setInterval(poll,4000); if(!v&&timer){clearInterval(timer);timer=null}}
async function togglePlay(){const on=$('playhere').checked;
  const r=await j('/api/return',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({on})});
  if(r&&typeof r.return_on==='boolean')$('playhere').checked=r.return_on}
async function signout(){await j('/api/signout',{method:'POST'});location.reload()}
async function req(){const r=await j('/api/signin-request',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({control_url:$('curl').value,email:$('email').value})});
  $('m1').textContent=r._error||r.note||''}
async function redeem(){const r=await j('/api/signin-redeem',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('code').value})});
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
  BR=b; $('bridge').innerHTML=b.map(x=>`<option value="${x.id}">${x.name} · ${x.pairing_code}`+
    `${x.online?'':' (offline)'}</option>`).join('');
  if(s.last_bridge)$('bridge').value=s.last_bridge;
  const d=await j('/api/devices');
  $('cam').innerHTML=(d.video||[]).map(x=>`<option>${x.name}</option>`).join('');
  $('mic').innerHTML=(d.audio||[]).map(x=>`<option>${x.name}</option>`).join('');
  if(s.last_camera)$('cam').value=s.last_camera; if(s.last_mic)$('mic').value=s.last_mic;
  for(const el of ['bridge','cam','mic'])$(el).onchange=remember;
}
function remember(){fetch('/api/remember',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({bridge_id:$('bridge').value,camera_name:$('cam').value,mic_name:$('mic').value})})}
async function unlock(){const h=host(); if(!h){$('m2').textContent='bridge has no reachable address';return}
  $('m2').textContent='unlocking…';
  const r=await j('/api/unlock',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({host:h,pin:$('pin').value})});
  $('m2').textContent=r._error||r.detail||r.result||JSON.stringify(r)}
async function golive(){
  if($('go').textContent==='End session'){await j('/api/stop',{method:'POST'});setLive(false);
    $('m2').textContent='session ended';return}
  const h=host(); if(!h){$('m2').textContent='bridge has no reachable address';return}
  $('m2').textContent='starting…';
  const r=await j('/api/golive',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({host:h,camera_name:$('cam').value,mic_name:$('mic').value})});
  if(r._error){$('m2').textContent=r._error;return}
  const warn = r.return_player!=='gstreamer';
  const via = r.via==='mesh' ? 'via secure mesh' : (r.via||'direct');
  $('m2').innerHTML=`live · ${r.camera} · ${r.mic} · <b>${via}</b><br>`+
    `<span style="color:${warn?'var(--red)':'var(--ok)'}">${r.return_note||''}</span>`;
  setLive(true); poll()}
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
  client_sees_camera:'The meeting laptop cannot see the camera. Re-seat the USB cable at the laptop end, then pick "NetBridge" as the camera in Zoom/Teams.',
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
function rescueReturnAudio(v){
  if(!v||v.ok)return v;
  const d=v.detail||'';
  if(/mismatch/i.test(d))return v;                // a real rate mismatch must stay red
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
let legRepairDone=false;
async function repairLegs(){
  if(legRepairDone)return; legRepairDone=true;
  $('m2').textContent='media path lost — reconnecting…';
  await j('/api/stop',{method:'POST'});
  // setLive(false) is NOT optional here. golive() branches on the BUTTON's label: while it
  // still reads "End session" it takes the teardown path, stops again and returns. The first
  // version of this omitted it, so the repair reliably ENDED the session and printed
  // "session ended" instead of reconnecting — turning a recoverable blip into a dead call.
  setLive(false);
  await golive();
}
async function poll(){
  const h=host(); if(!h)return; const c=await j('/api/checks?host='+h); if(c._error)return;
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
    $('ckfix').textContent='This app stopped sending — the '+lost+' path to the bridge dropped '
      +(legs.down_for_s||0)+'s ago. Your camera and mic are fine. Reconnecting…';
    $('ckfix').style.display='';
    repairLegs();
    return;
  }
  legRepairDone=false;   // healthy again: re-arm for the next outage
  // One instruction at a time — a wall of five red fixes is noise. The first broken link in
  // the chain is almost always the cause of the ones after it.
  $('ckfix').textContent=firstBad?FIXES[firstBad]||'':'';
  $('ckfix').style.display=firstBad?'':'none';}
boot();
</script>
"""


def main():
    # FIRST: install anything staged by a previous run. This happens before any port is
    # bound or any device is opened, so the app replaces itself with nothing in flight —
    # and re-execs, meaning this function runs again as the new build.
    apply_staged_update()
    # Reap anything orphaned by a previous crash/hard-quit before we start — a leftover mesh
    # helper holding the media ports would make the first go-live time out, and a leftover
    # ffmpeg/gst would keep the camera on / keep playing the room.
    _kill_orphan_mesh()
    _kill_orphan_media()
    # Watch the media legs for the life of the process. It self-gates on SESSION.live, so it
    # costs three failed binds every 3s while idle and nothing at all in attention.
    threading.Thread(target=LEGS.run, daemon=True).start()
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
