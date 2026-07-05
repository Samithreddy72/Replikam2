# Complete Setup Guide — zero to working bridge

*Written so that someone who has never touched a Raspberry Pi can follow it.*

## What you need (hardware)
| Item | Notes |
|---|---|
| Raspberry Pi 4 Model B | 2GB RAM or more |
| microSD card, 16GB+ | plus a way to plug it into your Mac (adapter) |
| **5V/3A power** into the Pi's GPIO pins (or a solid supply) | ⚡ weak power = random reboots when the laptop connects. If you ever see that: better adapter, thick short wires, or a 2200µF capacitor across GPIO pins 2(+)/6(−) |
| USB-C **data** cable | Pi's USB-C port → client laptop. Beware: many charging cables have no data lines |
| Your Mac (the sender) | with ffmpeg + GStreamer: `brew install ffmpeg gstreamer switchaudio-osx` |
| A Wi-Fi network both the Mac and Pi can join | |

## Step 1 — Flash the SD card (5 minutes)
1. Download **Raspberry Pi Imager** (raspberrypi.com/software) on your Mac
2. Choose Device: *Raspberry Pi 4* → Choose OS: **Raspberry Pi OS Lite (64-bit)** → Choose Storage: your SD card
3. Click **NEXT → EDIT SETTINGS** — this part matters:
   - **General tab:**
     - hostname: `bridge-001`
     - username: `pi` — password: pick one (you'll type it exactly once)
     - Wi-Fi: your network name + password (the same Wi-Fi your Mac uses)
   - **Services tab:** ✅ Enable SSH → "Use password authentication"
4. Save → Yes → wait for flashing to finish
5. Put the SD in the Pi, power it on, **wait 90 seconds** (first boot is slow)

## Step 2 — One command (10–15 minutes, automatic)
On your Mac:
```bash
git clone https://github.com/Samithreddy72/RepliKam2.git
cd RepliKam2
bash setup.sh bridge-001.local
```
- It asks for the Pi's password **once** (the one you set in the Imager)
- Then everything is automatic: packages → the pinned kernel → the bridge → reboot → self-check
- At the end you should see: `🏆 SETUP COMPLETE!`

**If `bridge-001.local` isn't found:** find the Pi's IP in your router's device list
(or `arp -a | grep -i "dc:a6\|e4:5f\|d8:3a"`) and use that instead: `bash setup.sh 192.168.x.x`

## Step 3 — Remote access (once, 1 minute)
```bash
ssh -i ~/.ssh/pi_bridge pi@bridge-001.local sudo tailscale up
```
Open the link it prints, sign in. Now the Pi is reachable from anywhere, forever.

## Step 4 — Go live!
1. **Plug the client laptop** into the Pi's USB-C port (do this BEFORE joining the meeting)
2. On the Mac:
   ```bash
   bash mac/go-live.sh
   ```
   Click **Allow** when macOS asks for camera/microphone.
3. On the client laptop, in the meeting app:
   | Setting | Choose |
   |---|---|
   | Camera | **UVC Camera** |
   | Microphone | **Microphone (Source/Sink)** |
   | Speakers | **Speakers (Source/Sink)** |
   | Noise suppression | **OFF** (the bridge already does it, better) |
4. To hear ALL the client's sounds on your Mac (recommended when nobody sits at the client):
   Win+R → `mmsys.cpl` → Playback → right-click **Speakers (Source/Sink)** → *Set as Default Device* and *Default Communication Device*
5. **Stopping:** `bash mac/stop-live.sh` (or make Desktop double-click buttons — see mac/ scripts)

## Daily use after setup
Power the Pi → it joins Wi-Fi and heals itself → plug client → go-live on the Mac. That's it.

## Something wrong?
→ [TROUBLESHOOTING.md](TROUBLESHOOTING.md) and [GOLDEN-RULES.md](GOLDEN-RULES.md)
