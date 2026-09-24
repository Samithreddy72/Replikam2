# NetBridge — verified working configuration (2026-09-24, 20:34 IST)

Verified live, read from the running processes on the Mac and the Pi. Nothing was changed to take it.
Presenter reports: smooth video, no freezes, no latency issues.

## Components
| Part | Version |
|---|---|
| Pi image | `2.1.0-52a161b` (GitHub draft release v2.1.0-52a161b, audited 107/107 + 54/54) |
| Pi camera service | signed update `97219e9c`: built-in behaviour + USB-miss counter, USB IRQ 34 on CPU 2 — now also the built-in default in the code (branch `opt/pi-2026-09-22`) |
| Mac app | NetBridge 1.4.3 — the default app in `~/Desktop/NetBridge` (older versions in its `Older versions` folder) |
| Mesh | Tailscale, direct path (Mac 100.91.142.38 ↔ Pi 100.67.196.104) |
| Profile | WAN (`NET_VIDEO_LATENCY=300`, `NET_AUDIO_LATENCY=300`); video capped at 100 ms by the feeder |

## VIDEO — Mac camera → Pi → meeting laptop (USB camera)
| Stage | Setting |
|---|---|
| Mac capture | MacBook Air camera, 1280×720 @ 30 fps, uyvy422 (AVFoundation) |
| Mac scale | 424×240, nv12 |
| Mac frame rate | 30 fps constant (cfr) — the camera's native rate, no 30→20 conversion |
| Mac encoder | Apple VideoToolbox H.264, real-time, **Baseline** profile, **600 kbps**, keyframe every 30 frames (1 s), SPS/PPS repeated on keyframes |
| Mac transport | RTP pt 96, ≤1100-byte packets → 127.0.0.1:5000 → mesh helper → Pi :5000 |
| Pi jitter buffer | **100 ms** (rtpjitterbuffer) |
| Pi decode | rtph264depay → h264parse → **avdec_h264 (software)**; hardware decoder off (gpu_mem=16) |
| Pi convert | videoconvert → YUY2 **424×240 @ 30 fps** (videoscale is a no-op, videorate 30) |
| Pi hand-off | v4l2loopback /dev/video40 `YUYV:424x240@30`, sync=false → uvc-gadget pump |
| USB camera (UVC) | uncompressed YUY2 **424×240, 30 fps** (dwFrameInterval 333333), frame 203,520 bytes |
| USB transport | isochronous, **1024-byte packets, one per 125 µs slot** (no high-bandwidth mode), USB 2.0 high-speed, 6.1 MB/s = 75% of the single-packet ceiling |
| Idle picture | black 424×240 (after 5 s without fresh video) |

## AUDIO — voice: Mac microphone → Pi → meeting laptop's microphone
| Stage | Setting |
|---|---|
| Mac capture | "System default microphone" (osxaudiosrc device 0), buffer 40 ms, period 10 ms; mic boost 0 dB; not muted |
| Mac format | 48 kHz stereo → S16LE |
| Mac encoder | **Opus 64 kbps**, audio-type voice, 20 ms frames, in-band FEC, 5% loss hint |
| Mac transport | RTP pt 97 → 127.0.0.1:5002 → mesh → Pi :5002 |
| Pi jitter buffer | **300 ms** (do-lost) |
| Pi decode | opusdec with PLC + in-band FEC |
| Pi processing | 48 kHz stereo S16LE → webrtcdsp: **AGC on** (target −6 dBFS, compression gain 6 dB, limiter on), high-pass filter on, noise suppression off, echo cancel off |
| Pi output | alsasink `plughw:UAC2Gadget`, buffer 200 ms, period 40 ms (no echo-cancel reference branch: AEC off) |
| USB mic to laptop (UAC2) | stereo (p_chmask 3), 16-bit, offers 48/44.1/32 kHz; **laptop opened 48 kHz stereo** (period 1024, buffer 9216 frames) |

## AUDIO — return: meeting laptop's speaker → Pi → Mac
| Stage | Setting |
|---|---|
| USB speaker from laptop (UAC2) | stereo (c_chmask 3), 16-bit, adaptive sync, offers 48/44.1/32 kHz; **laptop opened 48 kHz stereo** (period 960, buffer 9600 frames); rate follow on, no mismatch |
| Pi capture | alsasrc `hw:UAC2Gadget`, buffer 200 ms, period 20 ms → queue ≤300 ms (leaky) |
| Pi convert | audioresample quality 10 → 48 kHz stereo S16LE |
| Pi encoder | **Opus 128 kbps**, generic, in-band FEC, 20% loss hint → RTP pt 97 → Mac 100.91.142.38:5004 |
| Mac jitter buffer | **250 ms** (do-lost) |
| Mac decode/play | opusdec (FEC on, PLC off, concealment off) → resample q10 → volume 1.0 → osxaudiosink, sync on, buffer 200 ms, period 20 ms |

## Live verification (20:34 IST)
| Check | Result |
|---|---|
| Streams | video, voice, return all live; 0 restarts on every media service |
| Video to the laptop | 30 fps steady (59–61 frames per 2 s) |
| Mac → Pi packets | video 142,321 and voice 84,042 packets, **0 gaps** |
| Wi-Fi Mac ↔ Pi | avg 5.3 ms, max 10.7 ms round trip |
| Pi CPU | ~40% busy, load ~2 |
| Audio clock | "clean"; return rate 48 kHz, no mismatch |
| USB camera misses | ⚠️ 7–12 per second (not visible now; the source of earlier freezes) |
| Power | ⚠️ under-voltage 57–67% of the time; temp 46 °C |

## Estimated latency (glass-to-glass / mouth-to-ear)
| Path | Estimate |
|---|---|
| Video, Mac camera → meeting | ~210–275 ms (100 ms jitter buffer is the largest single item) |
| Voice, Mac mic → meeting | ~0.4–0.6 s (300 ms jitter buffer + audio device buffers) |
| Return, meeting → Mac | ~0.35–0.7 s (250 ms jitter buffer + buffers) |
Voice likely trails video by ~0.2–0.3 s (not reported as a problem).

## Watch items / pending (not changed)
- USB camera misses 7–12/s (counter at `/run/netbridge-usb-video.txt`). Tests ready: variant C (CPU 2 reserved for USB), variant B (IRQ on CPU 0).
- Power path (GPIO wiring/supply). Unplug the laptop before any camera-service restart (it reboots the Pi otherwise).
- Mac return player logged 3 "timestamp discontinuity, resyncing" warnings in ~11 min (brief gaps in the return stream).
- Next image: owner-key SSH. Already in the code: USB-miss counter built into the camera service; signed deploys flush files to the card before restarting.

## How to restore this exact setup
- Flash image 2.1.0-52a161b (Desktop › NetBridge-Image, or GitHub release v2.1.0-52a161b).
- Camera service: an image built from branch `opt/pi-2026-09-22` has it built in. On image 52a161b, deploy the signed update
  `97219e9c` (bridge-uvcd.sh) with the meeting laptop **unplugged**.
- On the Mac, double-click `Launch NetBridge.command` in `~/Desktop/NetBridge` (1.4.3, the default).
- Keep the WAN profile. Never switch the bridge to the LAN profile.
