# Persistent audio and diagnostic replay

## Presenter receiver

Source runs with PyGObject/GStreamer now use one in-process pipeline. `NB_AUDIO_ENGINE=legacy`
selects the previous `gst-launch` receiver. Missing GI falls back with an explicit diagnostics
message; pipeline startup/runtime failures are reported rather than presented as healthy audio.
Existing StreamGuard recovery still limits repeated failures and never restarts intentionally
stopped playback. Manual recovery restarts only return audio and is limited to once per 30 s.

Gain, dynamics, sink synchronization, FEC, PLC and jitter latency change through element
properties without destroying the pipeline, rebinding UDP, or restarting the mesh. Changing
latency/sync may still require a clock adjustment; persistent does not mean every possible
setting change is inaudible. Dynamics bypass uses unity compressor ratios. No new unverified
clock-drift controller or automatic latency ladder is enabled.

The receive buffer is separate from the output device buffer and network delay. The current
150 ms trial is not a measurement of total end-to-end latency. Queue depth, jitter statistics
and the selected clock are exposed so further reductions can be measured.

## Running locally

On macOS, `bash app/netbridge-source/setup-audio-dev.sh` installs the GI development runtime.
It uses Homebrew's GStreamer and a matching set of audio plugins, not relocated plugins from
a different release. `run-source.sh` prefers `.venv-gi` when available. The existing ffmpeg
and mesh helper are still selected through `NB_MEDIA_DIR`. Source changes require restarting
Python once; subsequent return tuning is live. Existing downloaded executables are unchanged.

Release builds can opt in with `build.py --persistent-audio` using the same GI/GStreamer
installation. This adds PyInstaller's Gst binding hook. Packaged Mac/Windows artifacts still
require platform-specific launch/device testing; source integration tests are not a substitute.

## Local API and UI

The Audio diagnostics panel shows the receiver state and offers explicit recording and
return-only recovery. The API stays on loopback and uses the existing origin/JSON checks for
mutations:

- `GET /api/audio/diagnostics`: packet arrivals, jitter-buffer lost/late/duplicate counts,
  selected clock, PCM discontinuities, queue depth, QoS, bounded recent warnings/errors.
- `POST /api/return-tuning`: existing keys; updates the persistent receiver in place.
- `POST /api/audio/capture` with `{"seconds":15}`: opt-in 1–60 s capture, only one at a time.
- `POST /api/audio/recover` with `{}`: restart return audio, without restarting video or mesh.
- `GET /api/audio/captures/<id>/<file>`: fixed filenames only; no arbitrary path access.

A forward RTP sequence gap is an observation, not confirmed loss: reordered packets can fill
it later. Use the jitter-buffer counters to inspect packets actually considered lost/late.
GStreamer `avg-jitter` is in nanoseconds; the explicit arrival-gap fields and queue depth use
milliseconds. PCM discontinuities may include startup and must be interpreted with timestamps.

## Capture contract

Recording is off by default. Captures are saved under `~/.netbridge-source/logs/captures/<id>`
with a private directory, up to 24 MiB per capture, a 256-item writer queue and a 60 s limit.
Disk writes run in a separate thread. Queue overflow invalidates the capture and is counted;
it does not block playback. Stop, error and expiry finalize the WAV. Captures are not uploaded
and may contain private meeting audio. Delete completed capture directories when no longer needed.

Files:

- `input.rtpdump`: original Opus RTP payloads and relative arrival times, in rtpplay1.0 format.
- `decoded.wav`: 48 kHz stereo S16 PCM after decode/resampling and before gain/dynamics/output.
- `timing.jsonl`: packet/PCM arrival offsets and PCM PTS. WAV alone does not retain PTS gaps.
- `manifest.json`: engine version, settings, byte counts, capture overflow/error status.

## NetEq evaluation

NetEq is not installed or used as the production receiver by this change. The evaluator
runs the real upstream `neteq_rtpplay` executable when supplied; there is no NetEq substitute.
Use an upstream WebRTC checkout and build its `neteq_rtpplay` target with the official build
tools. Preserve the checkout revision with benchmark results.

```sh
GST_PLUGIN_SYSTEM_PATH_1_0="$PWD/app/netbridge-source/_bundle/gi-plugins" \
  app/netbridge-source/.venv-gi/bin/python tools/evaluate-audio.py \
  /absolute/path/to/input.rtpdump --output /tmp/audio-comparison \
  --neteq /absolute/path/to/neteq_rtpplay --jitter-ms 150
```

Use a trace of at most 58 s so the bounded capture can include the receiver's drain interval.
The runner produces GStreamer and NetEq WAVs, a report containing trace/binary hashes, the
NetEq text log and replay scheduling error. Without `--neteq`, it runs the baseline, records
`not_run`, and exits 2. An invalid trace or missing output fails explicitly. Payload type 97
is mapped to Opus; inspect `--codec_map` for any conflicting mapping in the selected upstream
revision. Compare speech/music, startup delay, steady buffer delay, concealment and audible
artifacts. A shorter WAV is not proof of lower latency and these measurements are not PESQ/POLQA.
Replaying packet loss/jitter already present in the trace lets both implementations see the
same input. GStreamer replay uses wall-clock scheduling, whose error is included in the report.

References: [NetEq design](https://webrtc.googlesource.com/src/+/HEAD/modules/audio_coding/neteq/g3doc/index.md),
[upstream replay tool](https://webrtc.googlesource.com/src/+/714e3cbb48c704fe696e1f73f9a2f8dc0d2c0a16/modules/audio_coding/neteq/tools/neteq_rtpplay.cc).

## Pi remote tuning and recovery

`bridge return-tune` now writes `/data/config/bridge-return-tune`, on the writable data
partition. The return service reads it after the legacy `/etc/default` file. Clear writes
explicit empty properties so old read-only settings cannot silently reactivate. Write or
service restart failure propagates to the HTTP response. `GET /api/return-tune` returns the
parsed effective settings and configuration source for readback verification.

Deploy the CLI, return script and web service together. These changes have NOT been installed
on the live Pi: its existing administration/signing route is still required. A successful
configuration write alone does not prove the new pipeline reached PLAYING; verify service
logs, return stream counters and readback. This release does not add a remote arbitrary shell
or bypass the device's signed-update policy.

## September 16 USB diagnosis

The audible chipping was subsequently isolated to native Pi USB capture and removed by
switching the gadget's capture channel mask to stereo. See
[the measured A/B results and live deployment](USB-AUDIO-FIX-2026-09-16.md).
Manual `/api/return-tuning` jitter choices now persist across source-app restarts and take
precedence over fleet auto-tuning. Send `{"auto_jitter":true}` to release that choice.
