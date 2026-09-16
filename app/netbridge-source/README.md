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

Mac return playback defaults to synchronization on, gain 1.0 and compression off.
A September 2026 listening trial improved with these settings, but intermittent chipping
remains under investigation. `NB_RETURN_SINK_SYNC=0`, `NB_RETURN_GAIN=2.0` and
`NB_RETURN_DYNAMICS=1` restore the previous settings at launch. Windows defaults are
unchanged. The app's jitter buffer still defaults to 250 ms; fleet tuning can override it.

Persistent playback exposes warnings/errors and counters in **Audio diagnostics** and
`/api/audio/diagnostics`. Legacy GStreamer playback writes warnings/errors and active tuning to
`~/.netbridge-source/logs/netbridge-source-return.log`, retaining the preceding run in
`netbridge-source-return.log.previous`. `GST_DEBUG` can override the default warning
level (2) for a short diagnostic session. Logs are not packet-loss counters or audio
recordings. The state API now includes `return_dynamics` alongside gain and sync.
See [the audio review](../../docs/AUDIO-REVIEW-2026-09.md) for the next isolation test.

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

## Running from source

For local Mac development, run `bash app/netbridge-source/run-source.sh` from the repository
root. It uses `.venv/bin/python` and media tools in `_bundle/runtime` beside this README.
Override those locations with `NB_PYTHON` and `NB_MEDIA_DIR`. The runtime directory must
contain `ffmpeg`, `netbridge-mesh`, `gst/gst-launch-1.0`, `gst/plugins`, and their shared
libraries with their original relative paths. These dependencies are local, ignored files.
The Python environment needs `certifi` for HTTPS.

This workstation's runtime was extracted from the existing release, including its root
shared libraries, so source testing uses the same codecs. No application bundle is built.
Quit the release app first. The source app uses the same local account state and serves
the same port 8765; packaged-app auto-updates are disabled in source runs. Camera/microphone
permissions may need to be granted to the Python/terminal host on first use.

The watchdog checks return port 5004 only while local playback is enabled and identifies
it as the audio player's socket. This is not a check of the mesh's remote listener.
Repeated tuning values no longer restart playback merely because a new fleet request
arrived; actual changes still restart the player and can briefly interrupt sound.

## Not included

Code signing and notarization (needs a paid Apple account / EV certificate), auto-update
(depends on signing), and a bundled mesh client — the app reaches bridges over the network
you are already on.

## Persistent audio

See [persistent audio and diagnostics](../../docs/PERSISTENT-AUDIO.md) for the development runtime, live telemetry/capture API, recovery, Pi tuning changes and the NetEq replay evaluator.
