import { version as studioVersion } from "../package.json";
import { invoke } from "@tauri-apps/api/core";
import React, { useState, useEffect, useRef, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowRight,
  AudioLines,
  Check,
  ChevronDown,
  CircleHelp,
  Command,
  Download,
  Headphones,
  LayoutDashboard,
  LockKeyhole,
  Mic,
  MicOff,
  Monitor,
  MoreHorizontal,
  Play,
  Radio,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  Unplug,
  UserRound,
  Video,
  VideoOff,
  Mail,
  ChevronLeft,
  ChevronRight,
  Volume2,
  VolumeX,
  X,
  AlertCircle,
  LogOut,
  Camera,
  Terminal,
  Circle,
  CheckCircle2,
} from "lucide-react";
import {
  api,
  desktop,
  previewMode,
  bridgeHost,
  deliveryStatus,
  type Bridge,
  type Devices,
  type EngineState,
} from "./api";
import { Persona } from "./Persona";
import {Dialog} from "./Dialog";
import {sessionPage, saveSession, PAGE_SIZE, type HistoryEntry} from "./history";
import "./styles.css";
import "./studio.css";
import {AppearanceControl, BrandMark, Spinner, ExperienceFrame, Splash, WorkspaceLoading, NameEntry, Welcome, PreviewScenes, useAppearance, previewScene, profileKey, readProfile, storeProfile} from "./Experience";
import "./theme.css";

type Page = "Studio" | "Bridges" | "Sessions" | "Settings";
const meetingChecks = [
  { key: "video_arriving", name: "Your video arriving at bridge", icon: Video },
  { key: "voice_arriving", name: "Your voice arriving at bridge", icon: Mic },
  { key: "client_sees_camera", name: "Meeting laptop sees the camera", icon: Monitor },
  { key: "return_audio", name: "Meeting audio flowing back", icon: Headphones },
];
const emptyDevices: Devices = { video: [], audio: [] };
function App() {
  const {appearance,setAppearance}=useAppearance();
  const [startup,setStartup]=useState(!previewMode || previewScene==='splash');
  const [workspace,setWorkspace]=useState<"idle"|"loading"|"ready"|"error">("idle");
  const [workspaceError,setWorkspaceError]=useState("");
  const [workspaceAttempt,setWorkspaceAttempt]=useState(0);
  const [workspaceSlow,setWorkspaceSlow]=useState(false);
  const [profileRevision,setProfileRevision]=useState(0);
  const [welcome,setWelcome]=useState(false);
  const [namePreviewDone,setNamePreviewDone]=useState(false);
  const [sessionProfile,setSessionProfile]=useState<{key:string;name:string}|null>(null);
  const [nameDraft,setNameDraft]=useState("");
  useEffect(()=>{if(previewScene==='splash')return;const t=setTimeout(()=>setStartup(false),matchMedia('(prefers-reduced-motion: reduce)').matches?0:850);return()=>clearTimeout(t);},[]);
  const [page, setPage] = useState<Page>("Studio");
  const [state, setState] = useState<EngineState | null>(null);
  const [bridges, setBridges] = useState<Bridge[]>([]);
  const [devices, setDevices] = useState<Devices>(emptyDevices);
  const [selected, setSelected] = useState("");
  const [camera, setCamera] = useState("");
  const [mic, setMic] = useState("");
  const [pin, setPin] = useState("");
  const [unlocked, setUnlocked] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [noticeTone, setNoticeTone] = useState("info");
  const [pinError, setPinError] = useState("");
  const [videoOff, setVideoOff] = useState(false);
  const [micOff, setMicOff] = useState(false);
  const [inviteMode, setInviteMode] = useState(false);
  const [resendAt, setResendAt] = useState(0);
  const pinRef = useRef<HTMLInputElement>(null);
  const [engineError, setEngineError] = useState("");
  const [supportPreview, setSupportPreview] = useState<{report:Record<string,unknown>;notice:string}|null>(null);
  const [setupChecks, setSetupChecks] = useState<{label:string;status:string;detail:string}[]>([]);
  const [health, setHealth] = useState(false);
  const [source, setSource] = useState("Camera");
  const [email, setEmail] = useState("");
  const [control, setControl] = useState("https://fleet.scine.online");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [persona, setPersona] = useState(
    localStorage.getItem("nb.persona") || "illustrated",
  );
  const [preview, setPreview] = useState<MediaStream | null>(null);
  const [level, setLevel] = useState(0);
  const [audioDetails, setAudioDetails] = useState<Record<string, unknown>>({});
  const [palette, setPalette] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyPage, setHistoryPage] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [started, setStarted] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [quitPrompt, setQuitPrompt] = useState(false);
  const [update, setUpdate] = useState<{
    configured: boolean;
    version?: string;
    notes?: string;
  } | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const frame = useRef(0);
  const initialized = useRef(false);
  const opening = useRef(false);
  const bridge = bridges.find((b) => b.id === selected);
  const live = !!(state?.live || state?.wanted);
  const host = bridgeHost(bridge);
  const accountKey=profileKey(state?.email || "",state?.control_url || control);
  const storedProfile=sessionProfile?.key===accountKey ? {complete:true,name:sessionProfile.name} : readProfile(accountKey);
  const profile=previewMode && !['name','signin'].includes(previewScene) && !storedProfile.complete ? {complete:true,name:"Sam"} : storedProfile;
  const greeting=profile.name;
  const completeName=(name:string)=>{name=name.trim().slice(0,40);setSessionProfile({key:accountKey,name});storeProfile(accountKey,name);setProfileRevision(v=>v+1);setNamePreviewDone(true);setWelcome(true);};
  const experience=(content:React.ReactNode)=><ExperienceFrame appearance={appearance} onAppearance={setAppearance}>{content}</ExperienceFrame>;
  useEffect(()=>{setNameDraft(storedProfile.name);},[accountKey,profileRevision]);
  const stopPreview = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setPreview(null);
    cancelAnimationFrame(frame.current);
    void audioContext.current?.close();
    audioContext.current = null;
    setLevel(0);
  }, []);
  const action = async (label: string, fn: () => Promise<void>) => {
    if (opening.current) return;
    opening.current = true;
    setBusy(label);
    setNotice("");
    setNoticeTone("info");
    try {
      await fn();
    } catch (e) {
      setNoticeTone("error");
      setNotice(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
      opening.current = false;
    }
  };
  const refresh = useCallback(async () => {
    try {
      const s = await api<EngineState>("/api/state");
      setState(s);
      if(s.interruption && !s.wanted) {setNoticeTone("error");setNotice(s.interruption);}
      setEngineError("");
      if (!initialized.current) {
        setSelected(s.last_bridge || "");
        setCamera(s.last_camera || "");
        setMic(s.last_mic || "");
        if (s.control_url) setControl(s.control_url);
        initialized.current = true;
      }
      return s;
    } catch (e) {
      setEngineError(String(e instanceof Error ? e.message : e));
      return null;
    }
  }, []);
  const loadDevices = async () => {
    const d = await api<Devices>("/api/devices");
    setDevices(d);
    if (d.error) throw new Error(d.error);
    setCamera((c) =>
      d.video.some((v) => v.name === c) ? c : d.video[0]?.name || "",
    );
    setMic((m) => m === "System default microphone" || d.audio.some((v) => v.name === m) ? m : state?.system_default_mic ? "System default microphone" : d.audio[0]?.name || "");
  };
  const loadBridges = async () => {
    const b = await api<Bridge[]>("/api/bridges");
    setBridges(b);
    setSelected((s) => (b.some((v) => v.id === s) ? s : b[0]?.id || ""));
  };
  useEffect(() => {
    if (!desktop) return;
    let gone = false;
    const poll = async () => {
      if (gone) return;
      await refresh();
      if (!gone) timer = window.setTimeout(poll, 1000);
    };
    let timer = window.setTimeout(poll, 0);
    return () => {
      gone = true;
      clearTimeout(timer);
    };
  }, [refresh]);
  useEffect(() => {
    if (!state?.signed_in) {setWorkspace("idle");setWelcome(false);return;}
    let cancelled=false;
    setWorkspace("loading");setWorkspaceError("");setWorkspaceSlow(false);
    const slow=setTimeout(()=>{if(!cancelled)setWorkspaceSlow(true);},10000);
    // A completed sign-in owns exactly one load. Stale results from a previous
    // account or retry cannot replace the current workspace.
    void Promise.all([api<Bridge[]>("/api/bridges"),api<Devices>("/api/devices")]).then(([b,d])=>{
      if(cancelled)return;
      if(d.error)throw new Error(d.error);
      setBridges(b);setDevices(d);
      setSelected(s=>b.some(v=>v.id===s)?s:b[0]?.id || "");
      setCamera(c=>d.video.some(v=>v.name===c)?c:d.video[0]?.name || "");
      setMic(m=>m==="System default microphone" || d.audio.some(v=>v.name===m)?m:state.system_default_mic?"System default microphone":d.audio[0]?.name || "");
      setWorkspace("ready");
    }).catch(e=>{if(!cancelled){setWorkspaceError(String(e instanceof Error?e.message:e));setWorkspace("error");}}).finally(()=>clearTimeout(slow));
    return()=>{cancelled=true;clearTimeout(slow);};
  }, [state?.signed_in,accountKey,workspaceAttempt]);
  useEffect(() => {
    if (live) {setVideoOff(!!state?.video_muted);setMicOff(!!state?.voice_muted);}
  }, [live, state?.video_muted, state?.voice_muted]);
  useEffect(() => {
    if (page !== "Sessions" || !state?.signed_in) return;
    let cancelled = false; setHistoryLoading(true); setHistoryError("");
    sessionPage(historyPage).then(result => {
      if (!cancelled) {setHistory(result.entries);setHistoryTotal(result.total);}
    }).catch(() => {if (!cancelled) setHistoryError("Session history could not be loaded. Try reopening Sessions.");})
      .finally(() => {if (!cancelled) setHistoryLoading(false);});
    return () => {cancelled = true;};
  }, [page, historyPage, state?.signed_in]);
  useEffect(() => {
    if (videoRef.current) videoRef.current.srcObject = preview;
  }, [preview, page, source]);
  useEffect(() => () => stopPreview(), [stopPreview]);
  useEffect(() => {
    const t = setInterval(
      () =>
        setElapsed(
          started ? Math.floor((Date.now() - Date.parse(started)) / 1000) : 0,
        ),
      1000,
    );
    return () => clearInterval(t);
  }, [started]);
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPalette((v) => !v);
      }
      if (e.key === "Escape") {
        setPalette(false);
        setHealth(false);
      }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => {
    if (!desktop) return;
    let dispose: (() => void) | undefined;
    let gone = false;
    if (previewMode) return;
    import("@tauri-apps/api/window").then(async ({ getCurrentWindow }) => {
      const off = await getCurrentWindow().onCloseRequested(async (e) => {
        if (live || preview) {
          e.preventDefault();
          setQuitPrompt(true);
        }
      });
      if (gone) off();
      else dispose = off;
    });
    return () => {
      gone = true;
      dispose?.();
    };
  }, [live, preview]);
  const selectBridge = (id: string) => {
    setSelected(id);
    setUnlocked("");
    setPin("");
  };
  const remember = async () => {
    await api("/api/remember", {
      bridge_id: selected,
      camera_name: camera,
      mic_name: mic,
    });
  };
  const startPreview = async () => {
    if (previewMode) throw new Error("Local interface preview: camera and microphone are not opened.");
    if (live)
      throw new Error(
        "End your live session before testing camera or microphone capture.",
      );
    stopPreview();
    if (!navigator.mediaDevices?.getUserMedia)
      throw new Error(
        "Camera preview is unavailable in this webview. You can still stream using the bundled engine.",
      );
    let s: MediaStream | null = null;
    try {
      if (videoOff && micOff) throw new Error("Turn on your camera or microphone to preview it.");
      s = await navigator.mediaDevices.getUserMedia({video: !videoOff, audio: !micOff});
      const inputs = await navigator.mediaDevices.enumerateDevices();
      const selectedVideo = inputs.find(d => d.kind === "videoinput" && d.label === camera);
      const selectedAudio = inputs.find(d => d.kind === "audioinput" && d.label === mic);
      if ((!videoOff && selectedVideo) || (!micOff && mic !== "System default microphone" && selectedAudio)) {
        s.getTracks().forEach(t => t.stop());
        s = await navigator.mediaDevices.getUserMedia({
          video: videoOff ? false : selectedVideo ? {deviceId:{exact:selectedVideo.deviceId}} : true,
          audio: micOff ? false : mic !== "System default microphone" && selectedAudio ? {deviceId:{exact:selectedAudio.deviceId}} : true,
        });
      }
      if ((!videoOff && !selectedVideo) || (!micOff && mic !== "System default microphone" && !selectedAudio))
        setNotice("Preview uses a system device where exact matching is unavailable. Streaming uses your selected devices.");
      streamRef.current = s;
      setPreview(s);
      if (!s.getAudioTracks().length) return;
      const ctx = new AudioContext();
      audioContext.current = ctx;
      await ctx.resume();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      ctx.createMediaStreamSource(s).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      function sample() {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        data.forEach((v) => (sum += (v - 128) ** 2));
        setLevel(Math.min(1, Math.sqrt(sum / data.length) / 30));
        frame.current = requestAnimationFrame(sample);
      }
      sample();
    } catch (e) {
      s?.getTracks().forEach((t) => t.stop());
      stopPreview();
      throw e;
    }
  };
  const testSpeakers = async () => {
    if (previewMode) {setNotice("Local preview: no test tone played.");return;}
    const ctx = new AudioContext();
    try {
      await ctx.resume();
      const osc = ctx.createOscillator(),
        gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 440;
      gain.gain.setValueAtTime(0.08, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.6);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.65);
      await new Promise((r) => setTimeout(r, 750));
    } finally {
      await ctx.close();
    }
    setNotice(
      "Test tone played through the system output. Change the output in your operating system settings.",
    );
  };
  const endSession = async () => {
    await api("/api/stop", {});
    let historySaved = true;
    if (started) {
      try {await saveSession({started, ended: new Date().toISOString(), bridge: bridge?.name || "Bridge"});}
      catch {historySaved = false;}
    }
    setHistoryPage(0);
    setStarted("");
    setUnlocked("");
    await refresh();
    setNotice(historySaved ? "Session ended. Camera and microphone released." : "Session ended, but its history could not be saved on this device.");
    setNoticeTone(historySaved ? "success" : "warning");
  };
  const goLive = async () => {
    if (!host) throw new Error("Select a bridge with a reachable address.");
    setPinError("");
    if (!videoOff && !camera) throw new Error("Select a camera or turn the camera off to join with audio only.");
    if (source !== "Camera")
      throw new Error(
        "Avatar and screen transmission are not available in this build. Select Camera.",
      );
    stopPreview();
    await remember();
    if (unlocked !== host) {
      if (!pin.trim()) {setPinError("Enter the PIN provided by your fleet administrator.");pinRef.current?.focus();throw new Error("Bridge PIN is required. Enter it in Room & devices, then try again.");}
      const r = await api<{
        ok?: boolean;
        unlocked?: boolean;
        detail?: string;
        result?: string;
        message?: string;
      }>("/api/unlock", { host, pin }).catch(e => {setPinError(String(e.message || e));pinRef.current?.focus();throw e;});
      if (r.ok !== true && r.unlocked !== true) {
        setPinError(r.message || "PIN not accepted. Check the PIN and try again.");pinRef.current?.focus();
        throw new Error(
          r.message || r.detail || r.result || "PIN not accepted. Check the PIN and try again.",
        );
      }
      setUnlocked(host);
      setPin("");
    }
    const r = await api<{
      return_note?: string;
      return_player?: string;
      peer_result?: { _error?: string };
    }>("/api/golive", {
      video_muted: videoOff, voice_muted: micOff,
      host,
      camera_name: camera,
      mic_name: mic,
    }).catch((error) => {
      // A rejected/expired ticket must not trap retries behind cached UI unlock state.
      setUnlocked("");
      throw error;
    });
    setStarted(new Date().toISOString());
    await refresh();
    const audioProblem = r.peer_result?._error || r.return_player === "none";
    setNoticeTone(audioProblem ? "warning" : "success");
    setNotice(previewMode ? "Demo session only. No media is transmitted." : audioProblem
      ? "Your session started, but meeting audio needs attention. Open Connection health to check it."
      : "You’re live. Camera, microphone and meeting audio can be controlled below.");
  };
  const checkSetup = async () => {
    const result = await api<{checks:{label:string;status:string;detail:string}[]}>("/api/preflight", {
      bridge_id: bridge?.id, camera_name: camera, mic_name: mic, video_muted: videoOff, voice_muted: micOff,
    });
    setSetupChecks(result.checks);
  };
  const downloadReport = async () => {
    const a = await api<Record<string, unknown>>("/api/audio/diagnostics");
    setAudioDetails(a);
    const report = {
      generated_at: new Date().toISOString(),
      desktop_version: studioVersion,
      engine_version: state?.version,
      live: state?.live,
      delivery: Object.fromEntries(
        [
          "video_arriving",
          "voice_arriving",
          "return_audio",
          "client_sees_camera",
        ].map((k) => [k, deliveryStatus(state, k).label]),
      ),
      return_on: state?.return_on,
      return_gain: state?.return_gain,
      return_jitter_ms: state?.return_jitter_ms,
      audio_backend: a.backend,
      audio_running: a.running,
    };
    const saved = await invoke<string>("export_report", { report });
    setNotice(
      `Support report saved to ${saved}. No account details, credentials, or recordings included.`,
    );
  };
  const navigate = (next: Page) => {
    stopPreview();
    setPage(next);
    setPalette(false);
  };
  const changeSource = (next: string) => {
    stopPreview();
    setSource(next);
  };
  const button = (
    label: string,
    fn: () => Promise<void>,
    icon: React.ReactNode,
    className = "",
  ) => (
    <button
      className={className}
      disabled={!!busy}
      onClick={() => void action(label, fn)}
    >
      {icon}
      {label}
    </button>
  );
  const previewPanel = (personaOnly = false) => (
    <div className={`preview ${live ? "on-air" : ""}`}>
      <div className="preview-top">
        <span className="glass">
          <span className={`dot ${live ? "red" : ""}`} />
          {live
            ? state?.video_muted ? "Camera off · black output" : "Camera stream active"
            : personaOnly || source === "Avatar"
              ? "Avatar · local preview"
              : "Preview only"}
        </span>
        <span className="glass">
          {live ? "Room picture not verified" : "Not broadcasting"}
        </span>
      </div>
      {personaOnly || source === "Avatar" ? (
        <Persona kind={persona} level={level} />
      ) : preview && source === "Camera" ? (
        <video ref={videoRef} autoPlay playsInline muted />
      ) : (
        <div className="preview-empty">
          <div className="camera-orbit">
            {source === "Screen" ? (
              <Monitor size={36} />
            ) : live ? (
              <Radio size={36} />
            ) : (
              <Camera size={36} />
            )}
          </div>
          <h2>
            {live
              ? "Session running"
              : source === "Screen"
                ? "Your work, center stage."
                : "Camera preview"}
          </h2>
          <p>
            {live
              ? state?.video_muted ? "Your camera is off. The bridge receives plain black video." : "Your selected camera is sending to the bridge."
              : source === "Screen"
                ? "Screen transmission is planned for a future release."
                : "Check your camera and sound before joining the room."}
          </p>
          {!live &&
            source === "Camera" &&
            button(
              "Enable local preview",
              startPreview,
              <Video size={16} />,
              "preview-button",
            )}
          {live && (
            <span className="mini">
              Open connection health for bridge delivery checks.
            </span>
          )}
        </div>
      )}
      <div className="preview-bottom">
        <span>
          {personaOnly || source === "Avatar"
            ? "Avatar lab · not transmitted to the bridge"
            : live
              ? "Preview pauses during the session to keep camera capture reliable"
              : "Your preview stays on this device"}
        </span>
        {preview && (
          <button
            className="icon-button"
            aria-label="Stop preview"
            onClick={stopPreview}
          >
            <X size={16} />
          </button>
        )}
      </div>
    </div>
  );
  const deviceFields = (
    <>
      <label>
        Camera
        <select
          aria-label="Camera"
          value={camera}
          disabled={live || !!busy || videoOff}
          onChange={(e) => {
            stopPreview();
            setCamera(e.target.value);
            setUnlocked("");
          }}
        >
          {!devices.video.length && <option>No cameras loaded</option>}
          {devices.video.map((d) => (
            <option key={d.name}>{d.name}</option>
          ))}
        </select>
      </label>
      <label>
        Microphone
          <select
            aria-label="Microphone"
            value={mic}
            disabled={live || !!busy || micOff}
            onChange={(e) => {stopPreview();setMic(e.target.value);}}
          >
            {(state?.system_default_mic || !devices.audio.length) && (
              <option>System default microphone</option>
            )}
            {devices.audio.map((d) => (
              <option key={d.name}>{d.name}</option>
            ))}
          </select>
      </label>
      <div className="meter" aria-label="Local microphone test level">
        {Array.from({ length: 28 }, (_, i) => (
          <i key={i} className={i < level * 28 ? "lit" : ""} />
        ))}
      </div>
      <p className="field-note">
        {preview
          ? "Local microphone preview is active."
          : "Preview to check your microphone level."}
      </p>
      <div className="speaker-row"><span><Headphones size={15}/> System output</span>{button("Test speakers", testSpeakers, <Volume2 size={16}/>, "secondary")}</div>
    </>
  );
  const message = notice && <div className={`banner ${noticeTone}`} role={noticeTone === "error" ? "alert" : "status"}><AlertCircle size={18}/><span>{notice}</span><button className="icon-button" aria-label="Dismiss notification" onClick={()=>setNotice("")}><X size={16}/></button></div>;
  const requestCode = async () => {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) throw new Error("Enter a valid work email address.");
    if (new URL(control).protocol !== "https:") throw new Error("Use an HTTPS Fleet URL.");
    const r=await api<{note:string}>("/api/signin-request",{control_url:control,email:email.trim()});
    setSent(true);setCode("");setResendAt(Date.now()+60000);setNotice(r.note);
  };
  const redeemCode = async () => {
    if (!inviteMode && !/^[0-9]{6}$/.test(code)) throw new Error("Enter the six-digit code from your email.");
    if (!inviteMode && !email.includes("@")) throw new Error("Enter the email address that received your code.");
    await api("/api/signin-redeem",{control_url:control,email:email.trim(),code});
    setCode("");setSent(false);setNotice("");
    await refresh();
  };
  if (startup || (desktop && !state && !engineError)) return experience(<Splash/>);
  if (state?.signed_in && !live) {
    if (!profile.complete || (previewScene==='name' && !namePreviewDone)) return experience(<NameEntry onComplete={completeName}/>);
    if (workspace!=="ready" || previewScene==='connecting') return experience(<WorkspaceLoading error={workspaceError} slow={workspaceSlow} retry={()=>setWorkspaceAttempt(v=>v+1)}/>);
    if (welcome || previewScene==='welcome') return experience(<Welcome name={greeting} onContinue={()=>{setWelcome(false);if(previewScene==='welcome')location.href='/?preview=studio';}}/>);
  }
  if (!state?.signed_in) return experience(<div className="auth-shell">
    <main className="auth-card">
      <div className="auth-icon"><LockKeyhole size={25}/></div><span className="experience-kicker">A BETTER WAY TO CONNECT</span>
      <h1>{sent ? inviteMode ? "Use your invitation" : "Check your email" : "Welcome to NetBridge"}</h1>
      <p>{sent ? inviteMode ? "Paste the invitation code provided by your fleet administrator." : "Enter your six-digit code to open your workspace." : "Sign in to access your meeting bridges and set up your session."}</p>
      {!desktop && <div className="banner info">Interface preview. Open NetBridge Studio to connect to Fleet.</div>}
      {engineError && <div role="alert" className="banner error">{engineError}<button onClick={()=>void refresh()}>Retry</button></div>}
      {message}
      <form onSubmit={e=>{e.preventDefault();void action(sent ? "Signing in" : "Sending code",sent ? redeemCode : requestCode);}}>
        <label>Work email<input type="email" autoComplete="email" placeholder="you@company.com" value={email} required onChange={e=>{setEmail(e.target.value);setCode("");}}/></label>
        {sent && <label>{inviteMode ? "Invitation code" : "Sign-in code"}<input className="code-input" inputMode={inviteMode ? "text" : "numeric"} autoComplete="one-time-code" autoFocus maxLength={inviteMode ? 256 : 6} placeholder={inviteMode ? "Paste your invitation" : "000000"} value={code} onChange={e=>setCode(inviteMode ? e.target.value : e.target.value.replace(/[^0-9]/g,"").slice(0,6))}/></label>}
        <button className="primary wide" type="submit" disabled={!!busy || !desktop}>{busy ? <Spinner/> : sent ? <ArrowRight size={18}/> : <Mail size={18}/>} {sent ? "Sign in" : "Send sign-in code"}</button>
      </form>
      {sent ? <div className="auth-options">{!inviteMode && <button className="text-button" disabled={!!busy || Date.now()<resendAt} onClick={()=>void action("Sending code",requestCode)}>{Date.now()<resendAt ? "Resend available after 60 seconds" : "Resend code"}</button>}<button className="text-button" onClick={()=>{setSent(false);setInviteMode(false);setNotice("");setCode("");}}>Back</button></div> : <button className="text-button" onClick={()=>{setSent(true);setInviteMode(true);}}>Use an invitation code</button>}
      <details className="fleet-address"><summary>Fleet connection settings</summary><label>Fleet URL<input type="url" value={control} onChange={e=>{setControl(e.target.value);setSent(false);setCode("");}}/></label></details>
      <div className="auth-foot"><ShieldCheck size={15}/> Camera and microphone stay off until you choose.</div>
    </main>{previewMode && <p className="auth-caption">Try the preview with code 123456. No email is sent.</p>}
  </div>);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" onClick={() => navigate("Studio")}>
          <BrandMark/>NetBridge
          
        </a>
        <div className="workspace-label">YOUR WORKSPACE</div>
        <nav>
          {(
            [
              { name: "Studio", icon: LayoutDashboard },
              { name: "Bridges", icon: Radio },
              { name: "Sessions", icon: Activity },
            ] as const
          ).map(({ name, icon: Icon }) => (
            <button
              key={name}
              className={page === name ? "nav-active" : ""}
              onClick={() => navigate(name)}
            >
              <Icon size={18} />
              {name}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button className="shortcut" onClick={() => setPalette(true)}>
            <Command size={15} />
            Quick actions<kbd>⌘ K</kbd>
          </button>
          <button
            className={page === "Settings" ? "nav-active" : ""}
            onClick={() => navigate("Settings")}
          >
            <Settings size={18} />
            Settings
          </button>
          <div className="account">
            <div className="account-avatar">
              {(greeting || state?.email || "P")[0].toUpperCase()}
            </div>
            <div>
              <strong>{greeting || state?.email?.split("@")[0] || "Presenter"}</strong>
              <span>
                {state?.signed_in ? "Fleet account" : "Not signed in"}
              </span>
            </div>
            <span className={`dot ${state?.signed_in ? "green" : ""}`} />
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb"><PreviewScenes/>
            Workspace<span>/</span>
            <strong>{page}</strong>
          </div>
          <div className="topbar-right"><AppearanceControl value={appearance} onChange={setAppearance}/>
            {live ? (
              <span className="live-pill">
                <span className="dot red" />
                LIVE{" "}
                {Math.floor(elapsed / 60)
                  .toString()
                  .padStart(2, "0")}
                :{(elapsed % 60).toString().padStart(2, "0")}
              </span>
            ) : (
              <span className="muted tiny">
                {previewMode ? "SIMULATED" : desktop ? "" : "BROWSER PREVIEW"}
              </span>
            )}
            <button
              className={`health-toggle ${health ? "selected" : ""}`}
              onClick={() => setHealth((v) => !v)}
            >
              <Activity size={16} />
              Connection health
            </button>
            <button className="health-toggle" onClick={() => void action("Checking setup", checkSetup)}>Check my setup</button>
            <button className="health-toggle" onClick={() => void action("Preparing report", async () => setSupportPreview(await api("/api/support-report",{submit:false})))}>Get help</button>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {page === "Studio"
                  ? "PRESENTER"
                  : "NETBRIDGE WORKSPACE"}
              </div>
              <h1>
                {page === "Studio"
                  ? live
                    ? "Live session"
                    : greeting ? `Hi, ${greeting}!` : "Your Studio"
                  : page === "Bridges"
                      ? "Meeting bridges"
                      : page === "Sessions"
                        ? "Session history"
                        : "Settings"}
              </h1>
              <p>
                {page === "Studio"
                  ? live ? "Control your microphone and meeting audio here." : "Choose your room, check your devices, then go live."
                  : page === "Bridges"
                      ? "Choose the room where your next conversation happens."
                      : page === "Sessions"
                        ? "Sessions ended from this app are saved on this device."
                        : "Fine-tune your setup, account, and listening experience."}
              </p>
            </div>
            {page === "Studio" && (
              <span className="private-badge">
                <ShieldCheck size={15} />
                Private by design
              </span>
            )}
          </div>
          {page === "Studio" && (
            <section className="meeting-strip" aria-label="Meeting checks">
              <button className="strip-heading" onClick={() => setHealth(true)}><Activity size={16}/><strong>Meeting checks</strong><span>{meetingChecks.filter(({key}) => deliveryStatus(engineError ? null : state,key).tone === "good").length}/4 confirmed</span></button>
              <div className="strip-checks">{meetingChecks.map(({key,name,icon:Icon},i)=>{
                const check=deliveryStatus(engineError ? null : state,key);
                return <button key={key} onClick={()=>setHealth(true)} title={`${name}: ${check.label}`} aria-label={`${name}: ${check.label}`}><Icon size={16}/><span>{["Video","Microphone","Room camera","Return audio"][i]}</span><i className={`check-dot ${check.tone}`}/></button>;
              })}</div>
              <button className="text-button" onClick={()=>setHealth(true)} aria-label="View check details"><ChevronRight size={16}/></button>
            </section>
          )}
          {!desktop && (
            <div className="banner">
              <Monitor size={17} />
              Design preview · Open the desktop app to sign in and connect. No
              sample session is presented as live.
            </div>
          )}
          {engineError && (
            <div className="banner error" role="alert">
              <AlertCircle size={18} />
              <span>{engineError}</span>
              <button onClick={() => void refresh()}>Retry</button>
            </div>
          )}
          {message}
          {busy && (
            <div className="working" role="status">
              <Spinner/>
              {busy}…
            </div>
          )}
          {page === "Studio" && (
            <>
              <div className="studio-grid">
                <section className="stage">
                  {previewPanel()}
                  {<div className="toolbar" aria-label="Media controls">
                    <div className="toolbar-item"><button className={`round ${!(live ? state?.video_muted : videoOff) ? "mint" : ""}`} disabled={!!busy} aria-pressed={live ? !!state?.video_muted : videoOff} aria-label={(live ? state?.video_muted : videoOff) ? "Turn camera on" : "Turn camera off"} onClick={()=>{
                      stopPreview(); if (!live) {setVideoOff(v=>!v);return;}
                      void action("Updating camera",async()=>{await api("/api/video",{muted:!state?.video_muted});await refresh();});
                    }}>{(live ? state?.video_muted : videoOff) ? <VideoOff size={22}/> : <Video size={22}/>}</button><span>{(live ? state?.video_muted : videoOff) ? "Camera off" : "Camera on"}</span></div>
                    <div className="toolbar-item">
                      <button
                        className={`round ${!(live ? state?.voice_muted : micOff) ? "mint" : ""}`}
                        disabled={!!busy}
                        aria-label={(live ? state?.voice_muted : micOff) ? "Unmute microphone" : "Mute microphone"}
                        aria-pressed={live ? !!state?.voice_muted : micOff}
                        onClick={() => {
                          if (!live) {
                            stopPreview();setMicOff(v=>!v);
                            return;
                          }
                          void action("Updating microphone", async () => {
                            await api("/api/microphone", {
                              muted: !state?.voice_muted,
                            });
                            await refresh();
                          });
                        }}
                      >
                        {(live ? state?.voice_muted : micOff) ? (
                          <MicOff size={22} />
                        ) : (
                          <Mic size={22} />
                        )}
                      </button>
                      <span>
                        {(live ? state?.voice_muted : micOff) ? "Mic muted" : "Microphone"}
                      </span>
                    </div>
                    <div className="toolbar-item">
                      <button
                        className={`round ${state?.return_on ? "mint" : ""}`}
                        disabled={!live || !!busy}
                        aria-label={
                          state?.return_on
                            ? "Mute meeting audio"
                            : "Enable meeting audio"
                        }
                        onClick={() =>
                          void action("Updating playback", async () => {
                            await api("/api/return", { on: !state?.return_on });
                            await refresh();
                          })
                        }
                      >
                        {state?.return_on ? (
                          <Volume2 size={22} />
                        ) : (
                          <VolumeX size={22} />
                        )}
                      </button>
                      <span>Meeting audio</span>
                    </div>
                  </div>}
                  <div className="stage-foot">
                    <LockKeyhole size={13} />
                    Media travels over your private mesh.
                    <span>NETBRIDGE STUDIO</span>
                  </div>
                </section>
                <aside className="setup-card">
                  <div className="card-heading">
                    <h3>{live ? "Your session" : "Room & devices"}</h3>
                    <button className="icon-button" title="Refresh devices" aria-label="Refresh devices" disabled={!!busy || live} onClick={()=>void action("Refreshing devices",loadDevices)}><RefreshCw size={14}/></button>
                  </div>
                  <label>
                    Meeting bridge
                    <select
                      aria-label="Meeting bridge"
                      disabled={live}
                      value={selected}
                      onChange={(e) => selectBridge(e.target.value)}
                    >
                      {!bridges.length && <option>No bridges loaded</option>}
                      {bridges.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.name}
                          {b.online ? "" : " · offline"}
                        </option>
                      ))}
                    </select>
                  </label>
                  {!live && (
                    <label>
                      Bridge PIN
                      <div className="input-with-icon">
                        <LockKeyhole size={15} />
                        <input
                          type="password"
                          autoComplete="off"
                          inputMode="numeric"
                          placeholder="Enter your bridge PIN"
                          aria-label="Bridge PIN"
                          ref={pinRef}
                          aria-invalid={!!pinError}
                          aria-describedby={pinError ? "pin-error" : undefined}
                          value={pin}
                          onChange={(e) => {setPin(e.target.value);setPinError("");}}
                        />
                      </div>
                      {pinError && <span id="pin-error" className="field-error">{pinError}</span>}
                    </label>
                  )}
                  <div className="separator" />
                  {deviceFields}
                  <div className="session-action-area">
                    <p>{live ? "Ending the session stops your camera and microphone." : !state?.signed_in ? "Sign in above to continue." : !host ? "Choose a meeting bridge to continue." : !camera && !videoOff ? "Select a camera or turn it off to join with audio only." : videoOff ? "Joining with camera off. The room receives black video." : "Enter your bridge PIN, then go live when you’re ready."}</p>
                      <button
                        className={`session-action ${live ? "end-action" : "primary"}`}
                        disabled={
                          !!busy ||
                          !desktop ||
                          (!live &&
                            (!state?.signed_in ||
                              !host ||
                              (!camera && !videoOff) ||
                              source !== "Camera"))
                        }
                        aria-label={live ? "End session" : "Go live"}
                        onClick={() =>
                          void action(
                            live ? "Ending session" : "Connecting",
                            live ? endSession : goLive,
                          )
                        }
                      >
                        {live ? <Square size={18} /> : <Play size={18} />}
                        {live ? "End session" : "Go live"}
                      </button>
                  </div>
                </aside>
              </div>
            </>
          )}
          {page === "Bridges" && (
            <>
              <div className="section-actions">
                <span>{bridges.length} available bridges</span>
                {button(
                  "Refresh bridges",
                  loadBridges,
                  <RefreshCw size={15} />,
                )}
              </div>
              <div className="bridge-grid">
                {bridges.map((b) => (
                  <button
                    disabled={live}
                    className={`bridge-card ${selected === b.id ? "chosen" : ""}`}
                    key={b.id}
                    onClick={() => {
                      selectBridge(b.id);
                      navigate("Studio");
                    }}
                  >
                    <div className="bridge-card-top">
                      <div className="small-icon">
                        <Radio size={24} />
                      </div>
                      <span className={`status ${b.online ? "good" : ""}`}>
                        <span className={`dot ${b.online ? "green" : ""}`} />
                        {b.online ? "Online" : "Offline"}
                      </span>
                    </div>
                    <h2>{b.name}</h2>
                    <p>
                      {selected === b.id
                        ? "Selected for your next session"
                        : "Ready for your next conversation"}
                    </p>
                    <span className="bridge-card-footer">
                      Open in Studio
                      <ArrowRight size={18} />
                    </span>
                  </button>
                ))}
              </div>
              {!bridges.length && (
                <div className="empty-card">
                  <Radio size={35} />
                  <h2>No bridges loaded yet.</h2>
                  <p>Sign in from Studio, then refresh your fleet.</p>
                  <button onClick={() => navigate("Studio")}>
                    Back to Studio
                    <ArrowRight size={16} />
                  </button>
                </div>
              )}
            </>
          )}
          {page === "Sessions" && (
            <div className="panel">
              <div className="card-heading">
                <h3>Recent sessions</h3>
                <span className="muted tiny">ON THIS DEVICE</span>
              </div>
              {historyError && <p role="alert" className="field-error">{historyError}</p>}
              {historyLoading ? <p role="status">Loading sessions…</p> : history.length ? (
                <div className="session-list">
                  {history.map((s, i) => (
                    <div key={i}>
                      <div className="small-icon">
                        <Video size={19} />
                      </div>
                      <div>
                        <strong>{s.bridge}</strong>
                        <p>{new Date(s.started).toLocaleString()}</p>
                      </div>
                      <span>
                        {Math.max(
                          1,
                          Math.round(
                            (Date.parse(s.ended) - Date.parse(s.started)) /
                              60000,
                          ),
                        )}{" "}
                        min
                      </span>
                      <span className="status good">Ended</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty-card">
                  <Activity size={36} />
                  <h2>A fresh start.</h2>
                  <p>
                    Your completed sessions will appear here. No audio or video
                    is recorded.
                  </p>
                </div>
              )}
              <nav className="pagination" aria-label="Session history pages"><span>{historyTotal} sessions · Page {historyPage+1} of {Math.max(1,Math.ceil(historyTotal/PAGE_SIZE))}</span><button disabled={historyLoading || historyPage===0} onClick={()=>setHistoryPage(p=>p-1)}><ChevronLeft size={16}/> Previous</button><button disabled={historyLoading || (historyPage+1)*PAGE_SIZE>=historyTotal} onClick={()=>setHistoryPage(p=>p+1)}>Next <ChevronRight size={16}/></button></nav>
            </div>
          )}
          {page === "Settings" && (
            <div className="settings-grid">
              <section className="panel appearance-panel">
                <h3><UserRound size={18}/> Personal preferences</h3>
                <p>Make Studio feel at home.</p>
                <label>Display name<input value={nameDraft} maxLength={40} onChange={e=>setNameDraft(e.target.value)}/></label>
                <button className="secondary wide" onClick={()=>{setSessionProfile({key:accountKey,name:nameDraft.trim().slice(0,40)});storeProfile(accountKey,nameDraft);setProfileRevision(v=>v+1);setNoticeTone("success");setNotice("Your display name is saved on this computer.");}}>Save name</button>
                <div className="separator"/><h3>Appearance</h3>
                <AppearanceControl value={appearance} onChange={setAppearance}/>
                <p className="field-note">Auto follows your computer’s light or dark appearance.</p>
              </section>
              <section className="panel">
                <div className="card-heading">
                  <h3>
                    <Mic size={18} />
                    Audio & devices
                  </h3>
                </div>
                {deviceFields}
                {button(
                  "Refresh devices",
                  loadDevices,
                  <RefreshCw size={15} />,
                  "wide secondary",
                )}
                <p className="field-note">
                  Choose your microphone here, or use System default microphone on macOS
                  to follow System Settings. Speaker output follows your operating system.
                  The microphone button mutes outgoing capture during a session.
                </p>
              </section>
              <section className="panel">
                <h3>
                  <Headphones size={18} />
                  Meeting audio
                </h3>
                <p className="field-note">
                  Adjust return playback without ending the outgoing session.
                </p>
                <label>
                  Return volume · {Number(state?.return_gain || 1).toFixed(1)}×
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    defaultValue={state?.return_gain || 1}
                    disabled={!live || !!busy}
                    onPointerUp={(e) => {
                      const gain = Number(e.currentTarget.value);
                      void action("Setting volume", async () => {
                        await api("/api/return-tuning", { gain });
                        await refresh();
                      });
                    }}
                    onKeyUp={(e) => {
                      if (e.key.startsWith("Arrow")) {
                        const gain = Number(e.currentTarget.value);
                        void action("Setting volume", async () => {
                          await api("/api/return-tuning", { gain });
                          await refresh();
                        });
                      }
                    }}
                  />
                </label>
                <label>
                  Jitter buffer
                  <select
                    value={state?.return_jitter_ms || "250"}
                    disabled={!live || !!busy}
                    onChange={(e) => {
                      const jitter_ms = Number(e.target.value);
                      void action("Setting buffer", async () => {
                        await api("/api/return-tuning", { jitter_ms });
                        await refresh();
                      });
                    }}
                  >
                    {[0, 100, 150, 250, 400, 600, 1000].map((n) => (
                      <option key={n} value={n}>
                        {n} ms
                      </option>
                    ))}
                  </select>
                </label>
                {button(
                  "Use fleet audio tuning",
                  async () => {
                    await api("/api/return-tuning", { auto_jitter: true });
                    await refresh();
                  },
                  <RefreshCw size={15} />,
                  "secondary wide",
                )}
                {button(
                  "Restart meeting audio",
                  async () => {
                    await api("/api/audio/recover", {});
                    setNotice("Meeting audio restarted.");
                    await refresh();
                  },
                  <RefreshCw size={15} />,
                  "secondary wide",
                )}
                <div className="separator" />
                <h3>App updates</h3>
                <p className="field-note">
                  Studio {studioVersion} · Engine {state?.version || "—"}
                </p>
                <p className="field-note">
                  Signed updates replace the complete app and its media engine.
                  Installation is blocked during a session.
                </p>
                {button(
                  "Check for updates",
                  async () => {
                    if (!desktop)
                      throw new Error("Open the desktop app to check updates.");
                    const result = await invoke<{
                      configured: boolean;
                      version?: string;
                      notes?: string;
                    }>("check_update");
                    setUpdate(result);
                    setNotice(
                      !result.configured
                        ? "This development build has no signed update channel configured."
                        : result.version
                          ? `Version ${result.version} is available.`
                          : "You’re on the latest published version.",
                    );
                  },
                  <RefreshCw size={15} />,
                  "secondary wide",
                )}
                {update?.version && (
                  <>
                    <p className="field-note">
                      {update.notes ||
                        `Version ${update.version} is available.`}
                    </p>
                    <button
                      className="primary wide"
                      disabled={live || !!busy}
                      onClick={() =>
                        void action("Installing update", async () => {
                          stopPreview();
                          await invoke("install_update");
                        })
                      }
                    >
                      Install and restart
                    </button>
                  </>
                )}
                {update?.configured === false && (
                  <div className="lab-note">
                    A release endpoint and update signing key must be configured
                    before distribution.
                  </div>
                )}
              </section>
              <section className="panel">
                <h3>
                  <UserRound size={18} />
                  Account
                </h3>
                <p>{state?.email || "Not signed in"}</p>
                <p className="field-note">{state?.control_url || control}</p>
                {state?.signed_in ? (
                  button(
                    "Sign out",
                    async () => {
                      if (live) await endSession();
                      stopPreview();
                      await api("/api/signout", {});
                      setBridges([]);
                      await refresh();
                      navigate("Studio");
                    },
                    <LogOut size={15} />,
                    "secondary",
                  )
                ) : (
                  <button onClick={() => navigate("Studio")}>
                    Sign in
                    <ArrowRight size={16} />
                  </button>
                )}
                <div className="separator" />
                <h3>
                  <Terminal size={18} />
                  Support
                </h3>
                <p className="field-note">
                  Export a minimal status report. Account details, credentials
                  and recordings are excluded.
                </p>
                {button(
                  "Export support report",
                  downloadReport,
                  <Download size={15} />,
                  "secondary wide",
                )}
              </section>
            </div>
          )}
          <footer>
            <span>
              <span className={`dot ${state && !engineError ? "green" : ""}`} />
              {previewMode ? "Simulated workspace · No stream" : state && !engineError
                ? "Media engine connected"
                : desktop
                  ? "Connecting to media engine"
                  : "Design preview"}
            </span>
            <span>
              NETBRIDGE STUDIO <b>{previewMode ? "DESIGN REVIEW" : studioVersion}</b>
            </span>
            <button
              onClick={() => {
                navigate("Settings");
                setNotice(
                  "Local logs: ~/.netbridge-source/logs/desktop-engine.log",
                );
              }}
            >
              Help & diagnostics
              <ArrowRight size={12} />
            </button>
          </footer>
        </main>
      </div>
      {setupChecks.length > 0 && <Dialog title="Setup check" close={()=>setSetupChecks([])}>
        {message}<p className="dialog-intro">A quick check before you join. Nothing is recorded or broadcast.</p>
        <div className="setup-results">{setupChecks.map(c=><div className={`setup-result ${c.status}`} key={c.label}>{c.status === "pass" ? <CheckCircle2 size={20}/> : <AlertCircle size={20}/>}<div><strong>{c.label}</strong><p>{c.detail}</p></div><span>{c.status === "pass" ? "Ready" : c.status === "unknown" ? "Not verified" : "Check needed"}</span></div>)}</div>
        <div className="dialog-actions"><button onClick={()=>setSetupChecks([])}>Close setup results</button>{button("Run again",checkSetup,<RefreshCw size={16}/>,"primary")}</div>
      </Dialog>}
      {supportPreview && <Dialog title="Get help" close={()=>setSupportPreview(null)}>
        {message}<div className="help-intro"><div className="auth-icon"><CircleHelp size={26}/></div><div><h3>Send a report to your fleet administrator</h3><p>Share a snapshot so your admin can help you get connected.</p></div></div>
        <div className="support-summary"><span><Radio size={18}/> {bridge?.name || "No bridge selected"}</span><span><Activity size={18}/> {live ? "Session active" : "Not broadcasting"}</span></div>
        <p className="privacy-note"><ShieldCheck size={18}/>{supportPreview.notice}</p>
        <details className="report-details"><summary>Review report details</summary><pre>{JSON.stringify(supportPreview.report,null,2)}</pre></details>
        <div className="dialog-actions"><button onClick={()=>void action("Saving report",downloadReport)}><Download size={16}/>Save local report</button>{button("Send report",async()=>{const r=await api<{reference:string}>("/api/support-report",{submit:true,bridge_id:bridge?.id});setNoticeTone("success");setNotice("Sent: "+r.reference);setSupportPreview(null);},<ArrowRight size={16}/>,"primary")}</div>
      </Dialog>}
      {health && (
        <aside className="health-drawer">
          <div className="card-heading">
            <h3>Connection health</h3>
            <button
              className="icon-button"
              aria-label="Close diagnostics"
              onClick={() => setHealth(false)}
            >
              <X size={19} />
            </button>
          </div>
          <p className="field-note">
            Measurements from the bridge. Unknown is never shown as healthy.
          </p>
          {[
            { key: "video_arriving", name: "Video delivery", icon: Video },
            { key: "voice_arriving", name: "Voice delivery", icon: Mic },
            { key: "return_audio", name: "Return audio", icon: Headphones },
            { key: "client_sees_camera", name: "USB connection", icon: Monitor },
          ].map(({ key, name, icon: Icon }) => {
            const s = deliveryStatus(state, key);
            return (
              <div className="health-row" key={key}>
                <Icon size={19} />
                <div>
                  <strong>{name}</strong>
                  <span className={s.tone}>{s.label}</span>
                  <p>{s.detail}</p>
                </div>
                {s.tone === "good" ? (
                  <CheckCircle2 size={17} className="good" />
                ) : s.tone === "warn" ? (
                  <AlertCircle size={17} className="warn" />
                ) : (
                  <Circle size={14} />
                )}
              </div>
            );
          })}
          <div className="separator" />
          <div className="metric">
            <span>Check age</span>
            <code>
              {state?.bridge_checks?.age_s == null
                ? "—"
                : `${Math.round(state.bridge_checks.age_s)} s`}
            </code>
          </div>
          <div className="metric">
            <span>Local return player</span>
            <code>{state?.return_on ? "Enabled" : "Off"}</code>
          </div>
          <div className="metric">
            <span>Jitter buffer</span>
            <code>{state?.return_jitter_ms || "—"} ms</code>
          </div>
          <p className="field-note">
            {state?.guard?.last ||
              state?.bridge_checks?.last ||
              "Recovery events will appear here."}
          </p>
          {button(
            "Restart meeting audio",
            async () => {
              await api("/api/audio/recover", {});
              await refresh();
            },
            <RefreshCw size={15} />,
            "secondary wide",
          )}
          {button(
            "Export report",
            downloadReport,
            <Download size={15} />,
            "secondary wide",
          )}
          <details>
            <summary
              onClick={() =>
                void action("Reading diagnostics", async () =>
                  setAudioDetails(await api("/api/audio/diagnostics")),
                )
              }
            >
              Audio diagnostics
            </summary>
            <pre>{JSON.stringify(audioDetails, null, 2)}</pre>
          </details>
        </aside>
      )}
      {palette && (
        <div className="modal-backdrop" onClick={() => setPalette(false)}>
          <div
            className="command-palette"
            role="dialog"
            aria-modal="true"
            aria-label="Quick actions"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="card-heading">
              <h3>
                <Command size={18} />
                Quick actions
              </h3>
              <button
                aria-label="Close quick actions"
                className="icon-button"
                onClick={() => setPalette(false)}
              >
                <X size={18} />
              </button>
            </div>
            {(
              [
                "Studio",
                "Bridges",
                "Sessions",
                "Settings",
              ] as Page[]
            ).map((p) => (
              <button key={p} onClick={() => navigate(p)}>
                Open {p}
                <ArrowRight size={16} />
              </button>
            ))}
            <button
              onClick={() => {
                setHealth(true);
                setPalette(false);
              }}
            >
              Connection health
              <Activity size={16} />
            </button>
          </div>
        </div>
      )}
      {quitPrompt && (
        <div className="modal-backdrop">
          <div
            className="command-palette"
            role="dialog"
            aria-modal="true"
            aria-label="End session and quit"
          >
            <h2>Finish up and quit?</h2>
            <p>
              Quitting ends the session and releases your camera and microphone.
            </p>
            <button onClick={() => setQuitPrompt(false)}>Keep working</button>
            <button
              className="danger"
              onClick={() =>
                void action("Closing Studio", async () => {
                  if (live) await endSession();
                  stopPreview();
                  setQuitPrompt(false);
                  const { getCurrentWindow } =
                    await import("@tauri-apps/api/window");
                  await getCurrentWindow().destroy();
                })
              }
            >
              End session and quit
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
