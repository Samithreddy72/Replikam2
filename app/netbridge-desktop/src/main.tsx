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
  LoaderCircle,
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
  bridgeHost,
  deliveryStatus,
  type Bridge,
  type Devices,
  type EngineState,
} from "./api";
import { Persona } from "./Persona";
import "./styles.css";

type Page = "Studio" | "Bridges" | "Sessions" | "Settings";
type HistoryEntry = { started: string; ended: string; bridge: string };
const meetingChecks = [
  { key: "video_arriving", name: "Video received", icon: Video },
  { key: "voice_arriving", name: "Voice received", icon: Mic },
  { key: "client_sees_camera", name: "Meeting camera", icon: Monitor },
  { key: "return_audio", name: "Meeting audio", icon: Headphones },
];
const emptyDevices: Devices = { video: [], audio: [] };
function readHistory(): HistoryEntry[] {
  try {
    return JSON.parse(localStorage.getItem("nb.sessions") || "[]");
  } catch {
    return [];
  }
}
function App() {
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
  const [history, setHistory] = useState<HistoryEntry[]>(readHistory);
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
    try {
      await fn();
    } catch (e) {
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
      if(s.interruption && !s.wanted) setNotice(s.interruption);
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
    setMic((m) => m || d.audio[0]?.name || "");
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
    if (state?.signed_in) {
      void action("Loading workspace", async () => {
        await loadBridges();
        await loadDevices();
      });
    }
  }, [state?.signed_in]);
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
      mic_name: state?.system_default_mic ? "System default microphone" : mic,
    });
  };
  const startPreview = async () => {
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
      s = await navigator.mediaDevices.getUserMedia({
        video: source === "Camera",
        audio: true,
      });
      if (source === "Camera" && camera) {
        const inputs = await navigator.mediaDevices.enumerateDevices();
        const match = inputs.find(
          (d) => d.kind === "videoinput" && d.label === camera,
        );
        if (
          match &&
          s.getVideoTracks()[0]?.getSettings().deviceId !== match.deviceId
        ) {
          s.getTracks().forEach((t) => t.stop());
          s = await navigator.mediaDevices.getUserMedia({
            video: { deviceId: { exact: match.deviceId } },
            audio: true,
          });
        } else if (!match) {
          setNotice(
            "Preview uses the system camera; exact device matching is unavailable. Streaming uses the selected engine camera.",
          );
        }
      }
      streamRef.current = s;
      setPreview(s);
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
    if (started) {
      const next = [
        {
          started,
          ended: new Date().toISOString(),
          bridge: bridge?.name || "Bridge",
        },
        ...history,
      ].slice(0, 30);
      setHistory(next);
      localStorage.setItem("nb.sessions", JSON.stringify(next));
    }
    setStarted("");
    setUnlocked("");
    await refresh();
    setNotice("Session ended. Camera and microphone released.");
  };
  const goLive = async () => {
    if (!host) throw new Error("Select a bridge with a reachable address.");
    if (!camera) throw new Error("Select an available camera.");
    if (source !== "Camera")
      throw new Error(
        "Avatar and screen transmission are not available in this build. Select Camera.",
      );
    stopPreview();
    await remember();
    if (unlocked !== host) {
      if (!pin) throw new Error("Enter your bridge PIN first.");
      const r = await api<{
        ok?: boolean;
        unlocked?: boolean;
        detail?: string;
        result?: string;
        message?: string;
      }>("/api/unlock", { host, pin });
      if (r.ok !== true && r.unlocked !== true)
        throw new Error(
          r.message || r.detail || r.result || "Bridge refused the PIN.",
        );
      setUnlocked(host);
      setPin("");
    }
    const r = await api<{
      return_note?: string;
      peer_result?: { _error?: string };
    }>("/api/golive", {
      host,
      camera_name: camera,
      mic_name: state?.system_default_mic ? "System default microphone" : mic,
    });
    setStarted(new Date().toISOString());
    await refresh();
    setNotice(
      r.peer_result?._error
        ? `Streaming started, but return routing failed: ${r.peer_result._error}`
        : r.return_note ||
            "Session started. Waiting for bridge delivery checks.",
    );
  };
  const checkSetup = async () => {
    const result = await api<{checks:{label:string;status:string;detail:string}[]}>("/api/preflight", {
      bridge_id: bridge?.id, camera_name: camera, mic_name: mic,
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
            ? "Camera stream active"
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
              ? "Your selected camera is sending to the bridge."
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
          value={camera}
          disabled={live || !!busy}
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
        {state?.system_default_mic ? (
          <div className="select-like">
            <Mic size={15} />
            System default microphone
          </div>
        ) : (
          <select
            value={mic}
            disabled={live}
            onChange={(e) => setMic(e.target.value)}
          >
            {!devices.audio.length && (
              <option>System default microphone</option>
            )}
            {devices.audio.map((d) => (
              <option key={d.name}>{d.name}</option>
            ))}
          </select>
        )}
      </label>
      <div className="meter" aria-label="Local microphone test level">
        {Array.from({ length: 28 }, (_, i) => (
          <i key={i} className={i < level * 28 ? "lit" : ""} />
        ))}
      </div>
      <p className="field-note">
        {preview
          ? "Local test uses the system microphone."
          : "Enable preview to test your system microphone."}
      </p>
      <label>
        Speaker
        <div className="select-like">
          <Headphones size={15} />
          System audio output
        </div>
      </label>
      {button(
        "Test speakers",
        testSpeakers,
        <Volume2 size={16} />,
        "wide secondary",
      )}
    </>
  );
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" onClick={() => navigate("Studio")}>
          <span className="brand-mark">N</span>NetBridge
          
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
              {(state?.email || "D")[0].toUpperCase()}
            </div>
            <div>
              <strong>{state?.email?.split("@")[0] || "Presenter"}</strong>
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
          <div className="breadcrumb">
            Workspace<span>/</span>
            <strong>{page}</strong>
          </div>
          <div className="topbar-right">
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
                {desktop ? "DESKTOP STUDIO" : "BROWSER PREVIEW"}
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
          {supportPreview && <section className="card" aria-label="Support report preview">
            <h2>Send a report to your fleet administrator</h2><p>{supportPreview.notice}</p>
            <pre>{JSON.stringify(supportPreview.report,null,2)}</pre>
            <button onClick={() => void action("Sending report", async () => {
              const r=await api<{reference:string}>("/api/support-report",{submit:true,bridge_id:bridge?.id});
              setNotice("Sent: "+r.reference);setSupportPreview(null);
            })}>Send report</button>
            <button onClick={() => void downloadReport()}>Save local report instead</button>
            <button onClick={() => setSupportPreview(null)}>Cancel</button>
          </section>}
          {setupChecks.length > 0 && <section className="card" aria-label="Setup results" aria-live="polite">
            <h2>Setup check</h2>
            {setupChecks.map((c) => <p key={c.label}><strong>{c.label} — {c.status}</strong>: {c.detail}</p>)}
            <button onClick={() => setSetupChecks([])}>Close setup results</button>
          </section>}
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
                    : "Set up your session"
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
            <section className="meeting-checks panel" aria-label="Meeting checks">
              <div className="card-heading">
                <h3>Meeting checks</h3>
                <span className="muted tiny">{meetingChecks.filter(({key}) => deliveryStatus(engineError ? null : state, key).tone === "good").length}/4 confirmed</span>
              </div>
              <div className="meeting-check-grid">
                {meetingChecks.map(({key, name, icon: Icon}) => {
                  const check = deliveryStatus(engineError ? null : state, key);
                  return <div className="meeting-check" key={key}>
                    <Icon size={18} aria-hidden="true" />
                    <div><strong>{name}</strong><span className={check.tone}>{check.label}</span></div>
                  </div>;
                })}
              </div>
              <p className="field-note">Bridge delivery checks update while live. USB status alone cannot confirm the picture in the meeting app.</p>
              <button className="meeting-check-details" onClick={() => setHealth(true)}>View check details <ArrowRight size={14} /></button>
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
            <div className="banner warning">
              <AlertCircle size={18} />
              <span>{engineError}</span>
              <button onClick={() => void refresh()}>Retry</button>
            </div>
          )}
          {notice && (
            <div className="banner notice" role="status">
              <CircleHelp size={18} />
              <span>{notice}</span>
              <button
                aria-label="Dismiss message"
                className="icon-button"
                onClick={() => setNotice("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {busy && (
            <div className="working" role="status">
              <LoaderCircle size={14} className="spin" />
              {busy}…
            </div>
          )}
          {page === "Studio" && (
            <>
              {!state?.signed_in && (
                <section className="signin-card">
                  <div className="signin-intro">
                    <div className="small-icon">
                      <LockKeyhole size={19} />
                    </div>
                    <h3>1. Sign in to your fleet</h3>
                    <p>Use your work email to access the rooms shared with you.</p>
                  </div>
                  <div className="signin-fields">
                    <label>
                      Fleet URL
                      <input
                        type="url"
                        value={control}
                        onChange={(e) => setControl(e.target.value)}
                      />
                    </label>
                    <label>
                      Work email
                      <input
                        type="email"
                        autoComplete="email"
                        placeholder="you@company.com"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                      />
                    </label>
                    {button(
                      "Send sign-in code",
                      async () => {
                        if (!email.includes("@"))
                          throw new Error("Enter your work email.");
                        const url = new URL(control);
                        if (url.protocol !== "https:")
                          throw new Error("Use an HTTPS fleet URL.");
                        const r = await api<{
                          note: string;
                          raw?: { _error?: string };
                        }>("/api/signin-request", {
                          control_url: control,
                          email,
                        });
                        if (r.raw?._error) throw new Error(r.raw._error);
                        setSent(true);
                        setNotice(r.note);
                      },
                      <ArrowRight size={16} />,
                      "primary",
                    )}
                    {(sent || code) && (
                      <>
                        <label>
                          Sign-in or invitation code
                          <input
                            value={code}
                            onChange={(e) => setCode(e.target.value)}
                            autoComplete="one-time-code"
                          />
                        </label>
                        {button(
                          "Sign in",
                          async () => {
                            await api("/api/desktop/configure", {
                              control_url: control,
                            });
                            await api("/api/signin-redeem", { code });
                            setCode("");
                            await refresh();
                          },
                          <ArrowRight size={16} />,
                          "primary",
                        )}
                      </>
                    )}
                    {!sent && !code && (
                      <button
                        className="text-button"
                        onClick={() => setSent(true)}
                      >
                        I already have a code
                      </button>
                    )}
                  </div>
                </section>
              )}
              <ol className="setup-progress" aria-label="Session setup progress">
                <li className={state?.signed_in ? "complete" : "current"}><span>{state?.signed_in ? <Check size={14}/> : "1"}</span><div><strong>Sign in</strong><small>{state?.signed_in ? "Fleet account connected" : "Use your work email"}</small></div></li>
                <li className={state?.signed_in && !live ? "current" : live ? "complete" : ""}><span>{live ? <Check size={14}/> : "2"}</span><div><strong>Choose room & devices</strong><small>{bridge?.name || "Select your meeting bridge"}</small></div></li>
                <li className={live ? "current" : ""}><span>3</span><div><strong>{live ? "Session running" : "Go live"}</strong><small>{live ? "You control when to stop" : "Camera and microphone start only when you choose"}</small></div></li>
              </ol>
              <div className="studio-grid">
                <section className="stage">
                  {previewPanel()}
                  {live && <div className="toolbar" aria-label="Live audio controls">
                    <div className="toolbar-item">
                      <button
                        className={`round ${live && !state?.voice_muted ? "mint" : ""}`}
                        disabled={!!busy}
                        aria-label={
                          live
                            ? state?.voice_muted
                              ? "Unmute microphone"
                              : "Mute microphone"
                            : "Microphone settings"
                        }
                        onClick={() => {
                          if (!live) {
                            navigate("Settings");
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
                        {state?.voice_muted ? (
                          <MicOff size={22} />
                        ) : (
                          <Mic size={22} />
                        )}
                      </button>
                      <span>
                        {state?.voice_muted ? "Mic muted" : "Microphone"}
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
                    <h3>{live ? "Your session" : "2. Room & devices"}</h3>
                    <span className={`dot ${live ? "green" : ""}`} />
                  </div>
                  <label>
                    Meeting bridge
                    <select
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
                          value={pin}
                          onChange={(e) => setPin(e.target.value)}
                        />
                      </div>
                    </label>
                  )}
                  <div className="separator" />
                  {deviceFields}
                  {button(
                    "Refresh devices",
                    loadDevices,
                    <RefreshCw size={14} />,
                    "text-button wide",
                  )}
                  <div className="session-action-area">
                    <p>{live ? "Ending the session stops your camera and microphone." : !state?.signed_in ? "Sign in above to continue." : !host ? "Choose a meeting bridge to continue." : !camera ? "Connect and select a camera to continue." : "3. Enter the bridge PIN above, then start your session."}</p>
                      <button
                        className={`session-action ${live ? "end-action" : "primary"}`}
                        disabled={
                          !!busy ||
                          !desktop ||
                          (!live &&
                            (!state?.signed_in ||
                              !host ||
                              !camera ||
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
              {history.length ? (
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
            </div>
          )}
          {page === "Settings" && (
            <div className="settings-grid">
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
                  The media engine follows the macOS system microphone and
                  output. Select them in System Settings. The microphone button
                  in Studio mutes outgoing capture during a session.
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
              {state && !engineError
                ? "Media engine connected"
                : desktop
                  ? "Connecting to media engine"
                  : "Design preview"}
            </span>
            <span>
              NETBRIDGE STUDIO <b>{studioVersion}</b>
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
