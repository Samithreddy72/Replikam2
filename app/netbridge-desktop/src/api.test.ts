import { describe, it, expect } from "vitest";
import { deliveryStatus, bridgeHost, type EngineState } from "./api";
const live = {
  live: true,
  bridge_checks: {
    age_s: 2,
    checks: { video_arriving: { ok: true, detail: "Frames received" } },
  },
} as unknown as EngineState;
describe("bridge delivery evidence", () => {
  it("does not present idle, missing, or stale measurements as healthy", () => {
    expect(deliveryStatus(null, "video_arriving").tone).toBe("neutral");
    expect(
      deliveryStatus({ ...live, live: false }, "video_arriving").tone,
    ).toBe("neutral");
    expect(deliveryStatus(live, "voice_arriving").tone).toBe("neutral");
    expect(
      deliveryStatus(
        { ...live, bridge_checks: { ...live.bridge_checks, age_s: 45 } },
        "video_arriving",
      ).tone,
    ).toBe("neutral");
  });
  it("distinguishes fresh confirmed delivery from a failed check", () => {
    expect(deliveryStatus(live, "video_arriving").tone).toBe("good");
    expect(
      deliveryStatus(
        {
          ...live,
          bridge_checks: {
            age_s: 1,
            checks: { video_arriving: { ok: false } },
          },
        },
        "video_arriving",
      ).tone,
    ).toBe("warn");
  });
  it("never counts a configured USB flag as verified meeting reception", () => {
    const state = {...live, bridge_checks: {age_s: 1, checks: {client_sees_camera: {ok: true}}}};
    expect(deliveryStatus(state, "client_sees_camera").tone).toBe("neutral");
    expect(deliveryStatus({...state, voice_muted: true}, "voice_arriving").tone).toBe("neutral");
    expect(deliveryStatus({...state, return_on: false}, "return_audio").tone).toBe("neutral");
  });
  it("prefers the mesh destination and does not invent an address", () => {
    expect(
      bridgeHost({
        id: "1",
        name: "room",
        online: true,
        ip: "192.168.1.2",
        tailscale_ip: "100.1.2.3",
      }),
    ).toBe("100.1.2.3");
    expect(bridgeHost()).toBe("");
  });
});
