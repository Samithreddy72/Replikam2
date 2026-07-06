# Welcome to RepliKam — Developer Setup (10 minutes, once)

Your admin gave you a kit folder personalized for YOUR bridge. Three steps:

## 1. Join the network (once)
- Install Tailscale: https://tailscale.com/download
- Run the join command your admin sent you (it contains your access key), e.g.:
  - Mac: `tailscale up --authkey=tskey-...`  (or paste the key in the Tailscale app login)
  - Windows: same key in the Tailscale app
- You only ever see YOUR bridge — this is by design.

## 2. Requirements (once)
- **Mac**: `brew install ffmpeg gstreamer switchaudio-osx`
- **Windows**: nothing — the script auto-installs ffmpeg on first run

## 3. Go live (every time — one double-click)
- **Mac**: double-click **GO-LIVE.command** (click Allow for camera/mic the first time)
- **Windows**: right-click **go-live.ps1** → Run with PowerShell
- Stop with **STOP.command** / **stop-live.ps1**

That's it. The person running the meeting selects on their laptop:
Camera = **UVC Camera**, Mic = **Microphone (Source/Sink)**, Speakers = **Speakers (Source/Sink)**.

## If something's off
- Meeting says you're quiet → edit `developer.conf`, raise MIC_GAIN_DB to 12–14, restart
- Choppy audio at your end → your Wi-Fi; the bridge auto-adapts within a minute
- Anything else → tell your admin the time it happened (the bridge keeps flight logs)
