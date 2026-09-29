import { invoke, isTauri } from "@tauri-apps/api/core";
export type Bridge = {
  id: string;
  name: string;
  online: boolean;
  ip?: string;
  tailscale_ip?: string;
};
export type Devices = {
  video: { name: string; index: string }[];
  audio: { name: string; index: string }[];
  error?: string;
};
export type Check = { ok: boolean; detail?: string };
export type EngineState = {
  signed_in: boolean;
  email?: string;
  control_url: string;
  last_bridge?: string;
  last_camera?: string;
  last_mic?: string;
  system_default_mic?: boolean;
  live: boolean;
  wanted?: boolean;
  interruption?: string;
  voice_muted?: boolean;
  return_on: boolean;
  return_gain: string;
  return_jitter_ms: string;
  version: string;
  mesh?: { ok?: boolean; running?: boolean; detail?: string };
  bridge_checks?: {
    checks?: Record<string, Check>;
    age_s?: number;
    reachable?: boolean;
    last?: string;
  };
  guard?: { last?: string };
  update_note?: string;
};
export const desktop = isTauri();
export async function api<T>(path: string, body?: unknown): Promise<T> {
  if (!desktop)
    throw new Error(
      "Open NetBridge Studio to connect to the media engine. This browser view is a UI preview.",
    );
  const value = await invoke<T & { _error?: string; error?: string }>(
    "engine_request",
    { method: body === undefined ? "GET" : "POST", path, body: body ?? null },
  );
  if (value._error || value.error) throw new Error(value._error || value.error);
  return value;
}
export function bridgeHost(bridge?: Bridge) {
  return bridge?.tailscale_ip || bridge?.ip || "";
}
export function deliveryStatus(
  state: EngineState | null,
  key: string,
): { tone: "good" | "warn" | "neutral"; label: string; detail: string } {
  if (!state?.live)
    return {
      tone: "neutral",
      label: "Not streaming",
      detail: "Checks start when you go live.",
    };
  if (key === "voice_arriving" && state.voice_muted)
    return {
      tone: "neutral",
      label: "Microphone muted",
      detail: "Outgoing microphone capture is intentionally stopped.",
    };
  if (key === "return_audio" && state.return_on === false)
    return {tone:"neutral",label:"Playback disabled",detail:"You chose not to play meeting audio on this computer."};
  const age = state.bridge_checks?.age_s;
  const check = state.bridge_checks?.checks?.[key];
  if (
    age == null ||
    age > 20 ||
    !check ||
    state.bridge_checks?.reachable === false
  )
    return {
      tone: "neutral",
      label: age != null && age > 20 ? "Status is stale" : "Waiting for bridge",
      detail: "No fresh bridge measurement available.",
    };
  return {
    tone: check.ok ? "good" : "warn",
    label: check.ok ? (key === "client_sees_camera" ? "USB configured" : "Confirmed by bridge") : "Needs attention",
    detail: key === "client_sees_camera" ? "USB state only; the meeting app’s displayed picture is not verified." : check.detail || "No details provided",
  };
}
