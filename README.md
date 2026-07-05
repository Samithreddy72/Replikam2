# RepliKam2 🎥

**Turn a Raspberry Pi 4 into a driverless USB webcam + microphone + speaker for any laptop — fed by your Mac, over the network.**

Your camera and voice are wherever *you* are; the meeting runs on a laptop anywhere else. The Pi plugs into that laptop by USB and shows up as a completely standard webcam and headset — **nothing to install on the laptop**. Works with Google Meet, Zoom, and Microsoft Teams.

```
┌─────────┐   Wi-Fi / RTP    ┌──────────────┐    USB-C     ┌────────────────┐
│  YOUR   │ ───────────────► │ RASPBERRY PI │ ───────────► │ CLIENT LAPTOP  │
│  MAC    │  camera + voice  │   (bridge)   │  UVC camera  │  Meet / Zoom / │
│         │ ◄─────────────── │              │  UAC2 audio  │     Teams      │
└─────────┘  meeting audio   └──────────────┘              └────────────────┘
```

## ✨ What you get
- 📷 **Video**: your Mac's camera as "UVC Camera" on the client — 320×180 @ a locked 20 fps
- 🎙 **Your voice**: professionally processed on the Pi (noise suppression, auto-leveling,
  clip-proof limiting via Google's webrtc audio engine) → "Microphone (Source/Sink)"
- 🔊 **Meeting audio back to you**: everything the client plays (meeting voices, shared
  YouTube, system sounds) streams to your Mac, jitter-buffered and click-free
- 🛡 **Self-healing**: Wi-Fi guardian, hardware + service watchdogs, adaptive jitter
  buffers that auto-tune to network quality, power-loss-safe — power it on and forget it
- 🖱 **One-click start/stop** on the Mac, one-command setup for a fresh SD card

## 🚀 Quick start
1. **Read [docs/SETUP-GUIDE.md](docs/SETUP-GUIDE.md)** — flash the SD card (5 min, guided)
2. Run the one-click setup:
   ```bash
   git clone https://github.com/Samithreddy72/RepliKam2.git
   cd RepliKam2
   bash setup.sh bridge-001.local
   ```
3. Plug the Pi into the client laptop, run `bash mac/go-live.sh` on your Mac, join your meeting.

## 🧠 Hard-won engineering notes (why this repo exists)
| Problem | Solution shipped here |
|---|---|
| **Kernel ≥ 6.18 hard-freezes the Pi** the moment a laptop connects (dwc2 gadget bug) | pinned, battle-tested **kernel 6.12.93** installed by setup.sh, apt-held |
| The original uvc-gadget project vanished from GitHub | **patched, working pump binaries + sources preserved** in this repo (3 bugs fixed: memory-mode, buffer `field`, missing format negotiation) |
| v4l2loopback starves with default buffers; exclusive-caps quirks | pre-configured 16-buffer exclusive device, created automatically at boot |
| Wi-Fi drops, radio power-save, captive portals, corporate networks | **wifi-guardian**: multi-signal liveness, auto-reconnect ladder, self-reboot last resort |
| Audio jitter across changing networks | **jitter-sentry**: measures the path every 20 s, auto-switches buffer profiles |
| Voice too quiet / noisy / clipping | webrtcdsp chain: noise suppression + AGC + limiter, measured at broadcast density |

## 📁 Repository layout
```
setup.sh            ← the one-click fresh-SD installer (run from your Mac)
mac/                ← go-live.sh / stop-live.sh + senders & listener
pi/                 ← every bridge script, systemd unit, and config (tested versions)
restore/kernel/     ← kernel 6.12.93 .debs (the freeze fix — keep these safe!)
restore/binaries/   ← the patched uvc-gadget pump + library (irreplaceable)
sources/            ← patched source trees for future rebuilds
docs/               ← setup guide, golden rules, audio tuning, troubleshooting
```

## 📜 Golden rules
See [docs/GOLDEN-RULES.md](docs/GOLDEN-RULES.md). The big three:
1. **Never upgrade the kernel past 6.12.x** (setup pins it — leave the pin alone)
2. **Never unplug/replug the client mid-meeting** — plug once before joining
3. After any client replug, **re-check Windows sound devices** (replugs reset them)

---
*Built, broken, debugged, and rebuilt on real hardware. Every file in this repo has run in production.*
