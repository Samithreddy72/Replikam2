# Troubleshooting

**Start in the fleet, not in a terminal.** You do not need SSH, and there is no key to keep.

1. **Fleet → Actions → "Diagnose it for me"** — free, changes nothing, names the cause.
2. **Fleet → Actions → "Full diagnostics bundle"** — ~40 s, collects everything below.
3. **Fleet → Actions → "Read a file on the bridge…"** — read-only look at any of:

```
/home/pi/flight.txt      1s state snapshots; survives crashes and power loss
/home/pi/netlog.txt      wifi / tailscale state, 30s samples
/proc/asound/UAC2Gadget/pcm0c/sub0/status     the capture ring, live
```

> **Before you blame the bridge, check which end is failing.** The presenter app now says so
> itself when it gives up. A leg that keeps *dying* is a fault on the Mac; a leg that stays
> *up* while nothing arrives is the network or the bridge. Getting this backwards has cost
> this project more time than any other mistake.

| Symptom | Cause → Fix |
|---|---|
| Camera shows black | No video source running → open the NetBridge app and press **Go live**. Camera app opened before streams were up → close and reopen it. |
| Camera opens but sends nothing (green light never comes on; video red, voice and room audio fine) | The **macOS camera is wedged** — usually a process killed while it held the camera. Nothing on the bridge can fix this. On the Mac: `bash tools/fix-camera-macos.sh`, then **Stop** and **Go live** again so the leg is rebuilt. |
| "UVC Camera" missing on client | Charge-only USB cable (very common!) → use a data cable. Or gadget down → reboot the Pi. |
| Pi reboots when client plugs in | Under-powered → see Golden Rule 5. |
| Pi frozen / vanished from network | If on kernel 6.18 → that's the freeze bug, run setup.sh's kernel step. Otherwise power-cycle; guardian + watchdogs recover everything at boot. |
| Meeting can't hear you | Meeting app muted? Mic = "Microphone (Source/Sink)"? Windows mic level 100? Watch the app's mic meter while talking. |
| You can't hear the meeting | Client: Speakers (Source/Sink) as default. Mac: volume up; in the app, **Stop** then **Go live** rebuilds the listener. |
| Voice too quiet/loud | Adjust **return gain** in the app (clamped 0.2–4.0). The Pi-side chain adapts automatically. |
| Return audio stutters | **Fleet → Diagnose it for me** names the cause. Usually lateness (Wi-Fi bursts or under-voltage) → **deeper buffer on your side**, which does not touch video. The sentry does **not** raise it for you: that was removed on 2026-08-13 because it cannot fix late arrival, and a buffer silently growing mid-call hid the real fault. |
| Meeting hears YOU breaking up | The other direction — that buffer is on the BRIDGE. **Fleet → The ROOM cannot hear YOU → deepen the bridge buffer.** ⚠ ~5 s video freeze. |
| Everything green, but no room audio | The meeting laptop is not playing INTO NetBridge. Select NetBridge as its **speaker/output** there. This is not the microphone setting, and no fleet action can fix it. |
| Audio suddenly worse than yesterday | **Fleet → Config column.** `drift N` means something changed since the state you verified. Restore known-good. |
| Pi unreachable at a new location | It raises its own hotspot "BridgeSetup-XXXX" after ~60s offline → connect a phone to it and pick the new Wi-Fi. |
| Corporate/office Wi-Fi blocks everything | Client isolation — use a phone hotspot for the Pi + Mac, or ask IT to whitelist the Pi's MAC. |

---

## Which end is broken?

The single most useful question, because the two directions have **opposite** fixes:

```
room  --mic--> BRIDGE --network--> PRESENTER   buffered on the PRESENTER'S laptop
presenter --network--> BRIDGE --usb--> room    buffered on the BRIDGE
```

*You* cannot hear *them* → your buffer → **jitter-fix** (no video interruption).
*They* cannot hear *you* → the bridge's buffer → **jitter profile** (⚠ freezes video).

Getting this backwards costs a five-second video freeze and changes nothing you can hear.

## Under-voltage looks exactly like a network fault

Stutter with **0 % packet loss**. The SoC throttles, so packets arrive **late, not lost**.
No network change fixes it and no software fix exists — a deeper buffer masks it. Check
`power.rate` in the fleet row or `power.txt` in a bundle; the live `throttled` field on older
cards reads `0x0` even mid-brownout.
