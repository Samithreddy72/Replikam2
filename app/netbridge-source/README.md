# NetBridge Source — the presenter app

Runs on the presenter's machine and serves its UI on `127.0.0.1:8765` only.
**macOS and Windows.** No Python, no ffmpeg, no Tailscale install required.

## Download and run

Grab the zip for your platform from the release page, unzip, and run it.

### macOS — first run only
It is not code-signed, so Gatekeeper blocks it once:

* **Right-click the app → Open**, then confirm. (A normal double-click will not offer the option.)
* Or from a terminal, `cd` into the folder and run:  `xattr -dr com.apple.quarantine .`

  Clear the WHOLE folder, not just the app. `netbridge-mesh` is quarantined too, and a
  blocked helper does not announce itself: the meeting still sees and hears you, you hear
  nothing back, and nothing looks obviously wrong.

macOS will also ask for **Camera** and **Microphone** access the first time you go live —
this must be allowed, or ffmpeg opens the camera and silently receives no frames: the video
leg sits at ~0% CPU with an empty error log while the audio leg works fine.

### Windows — first run only
SmartScreen will warn about an unrecognised publisher: **More info → Run anyway**.
Windows Firewall may prompt for local network access; allow it on private networks.

## Using it

1. Enter your control-plane URL and sign in — a magic-link code, or the one-time invite an admin issued.
2. Pick bridge, camera and microphone. Choices are remembered **by name**, not by index:
   device indexes move when you plug in a headset, and a saved index silently streams the wrong camera.
3. Enter the bridge PIN your admin gave you offline. The **device** verifies it — three wrong
   tries locks the bridge for an hour.
4. **Go live.** Four checks turn green, each measuring a real link in the chain.
5. The meeting room's audio plays back on this machine.

## Troubleshooting

Each stream leg keeps its own stderr, so a dead leg can be explained rather than guessed at:

```
macOS    ~/.netbridge-source/logs/netbridge-source-{video,voice}.log
Windows  %USERPROFILE%\.netbridge-source\logs\netbridge-source-{video,voice}.log
```

**"Selected video size is not supported by the device"** — the camera rejected the capture
size. The app captures at 1280x720 and scales down for exactly this reason; if you see it,
the camera is unusual and the log names the modes it does support.

## Building it yourself

```bash
pip install pyinstaller
python3 build.py              # bundles ffmpeg
python3 build.py --no-ffmpeg  # smaller, expects ffmpeg on PATH
```

PyInstaller cannot cross-compile — a Windows binary must be built on Windows. CI does both
(`.github/workflows/build-app.yml`); push a tag `app-vX.Y.Z` to publish a release.

## Not included

Code signing and notarization (needs a paid Apple account / EV certificate), auto-update
(depends on signing), and a bundled mesh client — the app reaches bridges over the network
you are already on.
