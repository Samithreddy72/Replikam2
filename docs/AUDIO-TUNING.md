# RepliKam Audio — The Complete Picture (2026-07-05)

> Historical standalone-script recipe. The current app uses the embedded mesh and
> follows the meeting host's USB rate on the Pi. As of 2026-09-16 the Mac app defaults
> to synchronized playback, unity gain and dynamics off after a listening trial showed
> partial improvement. The standalone `mac-return-listen.sh` below still uses its older
> defaults. See [the current audio review](AUDIO-REVIEW-2026-09.md) for architecture,
> remaining uncertainty and the next diagnostic steps.

Both audio directions, every processing stage, every tuning knob, and why each exists.
Everything here is **deployed and verified live** on the current stack (kernel 6.12.93, video41 pipeline).

---

## The two audio paths

### ➡️ FORWARD — your voice → the meeting
```
Mac mic ──ffmpeg──► RTP :5002 ──► Pi bridge-feeder-audio ──► UAC2 gadget ──► client "Microphone (Source/Sink)" ──► Meet
```
| Stage | Where | What & why |
|---|---|---|
| Capture + pre-gain | `mac-stream.sh` | `volume=8dB` + `alimiter=0.9` — lifts the quiet Mac mic BEFORE Opus so encoding keeps detail; limiter makes it clip-safe |
| Encode | `mac-stream.sh` | Opus 64k stereo `lowdelay` (NEVER add ffmpeg `-fec 1` — it breaks the RTP muxer, mic crash-loops) |
| Auto-restart | `mac-stream.sh` | mic wrapped in a restart loop + crash-loop detector (a Wi-Fi blip = 1s gap, not dead mic) |
| Jitter buffer | Pi `bridge-feeder-audio.sh` | `rtpjitterbuffer latency=120 do-lost=true` — absorbs Wi-Fi timing; `opusdec plc=true` conceals lost packets with zero added latency |
| Noise gate | Pi | expander threshold `0.035` — silences the echo/bleed when you're not talking |
| Main gain | Pi | `volume=6.0` (+15.6dB) — so Meet/Teams noise gates don't swallow you |
| Safety | Pi | soft-knee compressor (0.45) + hard-knee brick-wall limiter (0.9) — loud speech cannot clip |
| Output | Pi | `alsasink UAC2Gadget sync=false` + 400ms leaky queue (anti-drift) |

### ⬅️ BACKWARD — meeting / shared YouTube → your ears
```
Meet ──► client "Speakers (Source/Sink)" ──► UAC2 gadget ──► Pi bridge-return-audio ──► RTP :5004 ──► Mac mac-return-listen.sh ──► your output device
```
| Stage | Where | What & why |
|---|---|---|
| Capture | Pi `bridge-return-audio.sh` | `hw:UAC2Gadget` at a **single locked 48kHz** (gadget `c_srate=48000`, the PR#2 fix — kills the 44.1k resample noise) |
| Encode | Pi | Opus **128k `audio-type=generic`** (music-grade for YouTube) + **inband FEC 20%** (survives packet loss) |
| Peer | Pi | sends to the Mac's Tailscale IP; `ts-warm` drop-in pings the peer before streaming (fixes the dormant-path "Network is unreachable" after Pi reboots) |
| **Jitter buffer** | Mac `mac-return-listen.sh` | **`rtpjitterbuffer latency=180`** — THE jitter fix: holds Wi-Fi timing bursts, releases steady. 180ms = low latency on strong signal; raise toward 300 if jitter returns |
| Decode | Mac | plain `opusdec` (no PLC on this side — tested cleaner, avoids synthetic-audio artifacts) |
| Voice boost | Mac | soft compressor threshold `0.12` — voices pass clean & boosted, loud media gets capped (not a real AGC, but close) |
| Gain + safety | Mac | `volume=2.0` + hard limiter `0.97` (raise gain via `RETURN_GAIN`; if loud music "pumps", lower toward 1.5) |
| **Anti-click sink** | Mac | **`osxaudiosink sync=false`** — THE anti-click fix: default sink drift-correction skips samples ~100×/s = audible clicking; sync=false plays the jitterbuffer's already-paced stream untouched |
| Auto-restart | Mac | listener self-restarts in ~2s if gst dies (used to die silently) |

---

## Tuning knobs (env vars, no file edits needed)
| Knob | Default | Use |
|---|---|---|
| `MIC_GAIN_DB=4` | 8 | your voice too loud/pumping → lower; too quiet → raise |
| `RETURN_GAIN=1.5` | 2.0 | return audio pumping on loud music → lower; too quiet → raise |
| `RETURN_JITTER_MS=300` | 180 | return audio stutters (weak Wi-Fi) → raise |
| `bridge profile wan` (on Pi) | lan (100/120ms) | across the internet → widens Pi jitter buffers to 200/200 |
| `FPS=15` | 20 | video knob (not audio) — steadier on weak links |

## Client (Windows/Meet) checklist
- Mic = **Microphone (Source/Sink)**, Speakers = **Speakers (Source/Sink)**, noise suppression **OFF**
- Never enable Windows "Listen to this device" on the Source/Sink pair (self-echo)
- Any USB replug can reshuffle Windows default devices — re-check after replugging the Pi

## Mac-side echo rule
Return audio binds the **default output at listener startup**. Run `go-live.sh` with the
**Bassheads plugged in** → it auto-routes in+out to them = echo-free. On laptop speakers,
the laptop mic hears the meeting = others hear themselves.

## Known-good measurements (for regression checks)
- Forward chain worst case: full-scale input caps at **-2.2dB** (cannot clip)
- Backward on strong Wi-Fi @180ms: **0 late / 0 lost / 0 dropouts / flat-factor 0** over 20s
- Healthy CPU: feeder-audio ~3-4%, return-audio ~6%, Mac listener light
