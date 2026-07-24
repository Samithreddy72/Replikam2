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


# --------------------------------------------------------------------------- session
class Session:
    """Owns the live ffmpeg legs + the return listener."""

    def __init__(self):
        self.procs = []
        self.logs = []
        self.bridge = None
        self.return_port = 5004
        self.return_player = "none"

    @property
    def live(self):
        return any(p.poll() is None for p in self.procs)

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
        logdir = _logdir()
        for name, argv in (("video", v), ("voice", a)):
            lf = open(os.path.join(str(logdir), "netbridge-source-%s.log" % name), "w")
            self.logs.append(lf)
            self.procs.append(subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=lf))

        self.return_player = self._start_return(return_port)

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
                self.procs.append(subprocess.Popen(
                    [gst, "-q", "udpsrc", "port=%d" % port, "caps=" + caps, "!",
                     "rtpjitterbuffer", "latency=250", "do-lost=true", "!",
                     "rtpopusdepay", "!", "opusdec", "plc=true", "use-inband-fec=true", "!",
                     "audioconvert", "!", "audioresample", "!", "autoaudiosink", "sync=false"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_gst_env()))
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
            self.procs.append(subprocess.Popen(
                [_ffmpeg(), "-hide_banner", "-loglevel", "warning",
                 "-protocol_whitelist", "file,udp,rtp", "-i", sdp_path] + out,
                stdout=subprocess.DEVNULL, stderr=lf))
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


SESSION = Session()


def _mesh_bin():
    """The embedded mesh client, bundled next to the app or built in mesh/ during dev."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    name = "netbridge-mesh.exe" if IS_WIN else "netbridge-mesh"
    for cand in (os.path.join(base, name), os.path.join(base, "mesh", name)):
        if _is_exe(cand):
            return cand
    return None


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
        if not (tsip and mesh_bin and st.get("token") and st.get("control_url")):
            self.stop()
            ip = (rec or {}).get("ip")
            return {"via": "direct", "control_host": ip, "control_port": 8080,
                    "media_host": ip, "return_peer": local_ip_towards(ip) if ip else ""}

        # reuse a live helper for the same bridge
        if self.proc and self.proc.poll() is None and self.bridge_id == rec.get("id"):
            return self._mesh_route()

        self.stop()
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
        self.control_port = int(hs.get("control_port") or self.CTRL_LOCAL)
        return self._mesh_route()

    def _mesh_route(self):
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


MESH = MeshManager()
_BRIDGES = {"list": [], "ts": 0.0}


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
from http.server import BaseHTTPRequestHandler, HTTPServer   # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                    # keep the presenter's terminal quiet

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This app talks to the local machine only; no page anywhere may script it.
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---------------- GET
    def do_GET(self):
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
            return self._send(out)
        if self.path.startswith("/api/checks"):
            host = self.path.split("host=", 1)[1] if "host=" in self.path else ""
            if not host:
                return self._send({"_error": "host required"}, 400)
            route = bridge_route(host, load_state())   # over the mesh when the bridge has one
            return self._send(api("GET", route["base"] + "/api/checks", timeout=10))
        return self._send({"_error": "not found"}, 404)

    # ---------------- POST
    def do_POST(self):
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
            route = bridge_route(host, st)
            r = api("POST", route["base"] + "/api/unlock", body={"pin": pin}, timeout=20)
            return self._send(r)

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
</style>
<div class=w>
<h1>NetBridge Source</h1><p class=sub>Sign in, unlock your bridge, go live.</p>
<div class=who><span id=who>not signed in</span>
  <span><button id=signout onclick=signout() style="display:none;width:auto;padding:3px 10px;font-size:11.5px;background:var(--ink);color:var(--pa)">sign out</button>
  <span id=livepill></span></span></div>

<div class=card id=signin>
  <label>Control plane URL</label><input id=curl placeholder="http://192.168.29.155:8000">
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
  <div class=row><span id=c3>Meeting laptop sees the camera</span><span class=lat id=l3></span></div>
  <div class=row><span id=c4>Meeting audio flowing back</span><span class=lat id=l4></span></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id); let BR=[],timer=null;
const j=(u,o)=>fetch(u,o).then(r=>r.json());
function host(){const b=BR.find(x=>x.id===$('bridge').value);return b?(b.tailscale_ip||b.ip||''):''}
async function boot(){
  const s=await j('/api/state');
  if(s.control_url)$('curl').value=s.control_url;
  if(s.signed_in){$('who').textContent='Signed in as '+(s.email||'');$('signout').style.display='';
    $('signin').style.display='none';$('main').style.display='';await load(s)}
  setLive(s.live)
}
function setLive(v){$('livepill').innerHTML=v?'<span class="pill on">● LIVE</span>':'';
  $('go').textContent=v?'End session':'Go live';$('health').style.display=v?'':'none';
  if(v&&!timer)timer=setInterval(poll,4000); if(!v&&timer){clearInterval(timer);timer=null}}
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
async function poll(){
  const h=host(); if(!h)return; const c=await j('/api/checks?host='+h); if(c._error)return;
  const map=[['c1','l1','online'],['c2','l2','video_arriving'],
             ['c3','l3','client_sees_camera'],['c4','l4','return_audio']];
  for(const [ci,li,k] of map){const v=c[k]||{};
    $(ci).className=v.ok?'ok':'bad'; $(li).textContent=(v.detail||'').slice(0,42)}}
boot();
</script>
"""


def main():
    st = load_state()
    if not st.get("control_url") and len(sys.argv) > 1:
        st["control_url"] = sys.argv[1].rstrip("/")
        save_state(st)
    srv = HTTPServer((HOST, PORT), Handler)
    url = "http://%s:%d/" % (HOST, PORT)
    print("NetBridge Source  ->  %s" % url)
    print("(local only; ctrl-c to quit)")
    _open_browser(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.stop()          # never leave ffmpeg holding the camera
        MESH.stop()             # remove the ephemeral mesh node
        print("\nstopped.")


if __name__ == "__main__":
    main()
