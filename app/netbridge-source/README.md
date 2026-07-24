# NetBridge Source — the presenter app (walkthrough Journey 3)

Runs on the presenter's machine, serves its UI on `127.0.0.1:8765` only.

```bash
python3 app/netbridge-source/source_app.py [CONTROL_URL]
```

## What it implements

| J3 step | Status |
|---|---|
| 2 Sign in with your work email | magic-link code **or** first-time invite |
| 3 Pick bridge, camera, mic | dropdowns; remembered **by name**, never by index (ledger E4) |
| 4 Unlock the bridge with its PIN | verified **on the device** via `/api/unlock` |
| 5 Go live, four checks | spawns the streams, polls `/api/checks` |
| 6 Hear the room back | registers this machine as the return peer, plays the return stream |

## Not done here
* **Packaging/signing** (ledger M7) needs an Apple Developer account, and Windows (M9) is a second platform.
* **Embedded mesh client** (J3 phase 5) — the app currently reaches bridges by LAN/tailnet address rather than bundling Tailscale.
* Auto-update — depends on signing.

## Notes worth keeping
* The ffmpeg invocations are copied from `mac/mac-stream.sh` deliberately. Capturing at
  `320x180`/`640x360` fails — *"Selected video size is not supported by the device"* — and
  the video leg dies instantly while the audio leg survives, which reads like a network
  fault rather than a bad argument. Capture at 1280x720 and scale.
* `-bsf:v dump_extra=freq=keyframe` repeats SPS/PPS so a late-joining receiver can decode.
* Opus `-fec 1` is deliberately absent: ffmpeg's RTP muxer rejects it and the mic leg
  crash-loops.
* Each leg's stderr goes to `/tmp/netbridge-source-{video,voice}.log` so a dead leg can be
  explained rather than guessed at.
