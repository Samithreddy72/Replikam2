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
import json, os, pathlib, shutil, socket, subprocess, sys, threading, time
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


# --------------------------------------------------------------------------- devices
def av_devices():
    """Enumerate cameras and mics BY NAME via ffmpeg's avfoundation lister."""
    if not shutil.which("ffmpeg"):
        return {"video": [], "audio": [], "error": "ffmpeg not found"}
    try:
        p = subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:
        return {"video": [], "audio": [], "error": str(e)}
    video, audio, section = [], [], None
    for line in (p.stderr or "").splitlines():
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
        self.bridge = None
        self.return_port = 5004

    @property
    def live(self):
        return any(p.poll() is None for p in self.procs)

    def start(self, pi_host, video_idx, audio_idx, fps=20, mic_gain=8, return_port=5004):
        self.stop()
        self.bridge, self.return_port = pi_host, return_port
        common = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        # These are mac/mac-stream.sh's PROVEN invocations, not re-derived ones. Two things
        # here were learned the hard way and must not be "simplified":
        #   * capture at 1280x720/uyvy422 and scale down. Asking the camera for 320x180 or
        #     640x360 fails outright - "Selected video size is not supported by the device"
        #     - and the leg dies instantly while the audio leg keeps running, which looks
        #     like a network fault rather than a bad argument.
        #   * -bsf:v dump_extra=freq=keyframe repeats SPS/PPS on every keyframe. Without it
        #     a receiver that joins late never gets the decoder config and shows nothing.
        # Opus FEC (-fec 1) is deliberately absent: ffmpeg's RTP muxer rejects it and the
        # mic leg crash-loops (2026-06-27).
        v = common + ["-f", "avfoundation", "-framerate", "30",
                      "-video_size", "1280x720", "-pixel_format", "uyvy422",
                      "-i", "%s:none" % video_idx,
                      "-vf", "scale=320:180,format=nv12", "-fps_mode", "cfr", "-r", str(fps),
                      "-c:v", "h264_videotoolbox", "-realtime", "1", "-b:v", "400k",
                      "-g", str(fps), "-bsf:v", "dump_extra=freq=keyframe", "-an",
                      "-f", "rtp", "rtp://%s:%d?pkt_size=1100" % (pi_host, RTP_VIDEO)]
        a = common + ["-f", "avfoundation", "-i", ":%s" % audio_idx,
                      "-af", "volume=%ddB,alimiter=limit=0.9" % mic_gain,
                      "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "2",
                      "-application", "lowdelay", "-payload_type", "97",
                      "-f", "rtp", "rtp://%s:%d" % (pi_host, RTP_VOICE)]
        # Keep each leg's stderr so a dead leg can be explained instead of guessed at.
        self.logs = []
        for name, argv in (("video", v), ("voice", a)):
            lf = open("/tmp/netbridge-source-%s.log" % name, "w")
            self.logs.append(lf)
            self.procs.append(subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=lf))
        # return-audio listener (gstreamer's jitter buffer absorbs WiFi timing variance)
        gst = shutil.which("gst-launch-1.0")
        if gst:
            caps = ("application/x-rtp,media=audio,encoding-name=OPUS,"
                    "payload=97,clock-rate=48000")
            self.procs.append(subprocess.Popen(
                [gst, "-q", "udpsrc", "port=%d" % return_port, "caps=" + caps, "!",
                 "rtpjitterbuffer", "latency=250", "do-lost=true", "!",
                 "rtpopusdepay", "!", "opusdec", "plc=true", "use-inband-fec=true", "!",
                 "audioconvert", "!", "audioresample", "!", "autoaudiosink", "sync=false"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

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
            return self._send(api("GET", "http://%s:8080/api/checks" % host, timeout=8))
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
            # Verified ON THE DEVICE. The control plane is not asked and cannot override it.
            r = api("POST", "http://%s:8080/api/unlock" % host, body={"pin": pin}, timeout=15)
            return self._send(r)

        if self.path == "/api/golive":
            host = b.get("host")
            if not host:
                return self._send({"_error": "host required"}, 400)
            devs = av_devices()
            vidx, vname, _ = resolve_by_name(devs.get("video", []), b.get("camera_name"))
            aidx, aname, _ = resolve_by_name(devs.get("audio", []), b.get("mic_name"), "0")
            port = int(b.get("return_port") or 5004)
            # Tell the bridge where to send the room back. Registered through the control
            # plane in production; the device endpoint is the same contract.
            me = local_ip_towards(host)
            peer = api("POST", "http://%s:8080/api/set-peer" % host,
                       body={"ip": me, "port": port}, timeout=15) if me else {"_error": "no route"}
            SESSION.start(host, vidx, aidx, return_port=port)
            st.update({"bridge_host": host, "camera_name": vname, "mic_name": aname})
            save_state(st)
            return self._send({"ok": True, "camera": vname, "mic": aname,
                               "return_peer": me, "return_port": port, "peer_result": peer})

        if self.path == "/api/stop":
            SESSION.stop()
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
<div class=who><span id=who>not signed in</span><span id=livepill></span></div>

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
  if(s.signed_in){$('who').textContent='Signed in as '+(s.email||'');
    $('signin').style.display='none';$('main').style.display='';await load(s)}
  setLive(s.live)
}
function setLive(v){$('livepill').innerHTML=v?'<span class="pill on">● LIVE</span>':'';
  $('go').textContent=v?'End session':'Go live';$('health').style.display=v?'':'none';
  if(v&&!timer)timer=setInterval(poll,4000); if(!v&&timer){clearInterval(timer);timer=null}}
async function req(){const r=await j('/api/signin-request',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({control_url:$('curl').value,email:$('email').value})});
  $('m1').textContent=r._error||r.note||''}
async function redeem(){const r=await j('/api/signin-redeem',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('code').value})});
  if(r._error){$('m1').textContent=r._error;return} location.reload()}
async function load(s){
  const b=await j('/api/bridges'); if(b._error){$('m2').textContent=b._error;return}
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
  $('m2').textContent=`live · ${r.camera} · ${r.mic} · room returns to ${r.return_peer}:${r.return_port}`;
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
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.stop()          # never leave ffmpeg holding the camera
        print("\nstopped.")


if __name__ == "__main__":
    main()
