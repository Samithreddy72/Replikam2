# Audio architecture review — 2026-09-16

## What the listening trial established

Enabling Mac return sink synchronization improved the reported sound slightly. Bypassing
the compressor/limiter chain and reducing gain from 2.0 to 1.0 improved it further. Chipping
remained. Gain and dynamics changed together, so their individual contributions are unknown.
This supports preserving the better settings, but does not establish a clock fault.

The Mac app now defaults to sync on, unity gain and dynamics off, with the existing
environment and live API overrides. Other platforms retain their defaults. The observed
600 ms buffer was a fleet override, not a successful isolated buffer experiment; the
default stays 250 ms. The running downloaded executable needs rebuilding/replacing to
include these source changes; live API tuning is not an application update.

The QuickTime recording was an output-side recording, not a tap before playback. Its
approximately -18.8 dBFS peak and absence of obvious 10 ms silence dips do not exclude
upstream distortion or lost audio. The precise QuickTime input device was not verified.

## Current paths

```mermaid
flowchart LR
    M[Presenter microphone] --> F[ffmpeg: gain, limiter, Opus]
    F --> V[Mesh UDP 5002]
    V --> P[Pi: jitter buffer, decode, voice DSP, gain]
    P --> U[USB gadget microphone]
    U --> C[Meeting laptop]
    C --> S[USB gadget speaker]
    S --> A[Pi ALSA capture: follows host rate]
    A --> E[Resample to 48 kHz, Opus with FEC]
    E --> R[Mesh UDP 5004, local forwarding queue]
    R --> J[Mac jitter buffer, Opus decode]
    J --> D[Optional dynamics, gain]
    D --> O[CoreAudio output]
```

Video has a separate ffmpeg/mesh/USB path on port 5000, but shares CPU, power and networking.
The control plane handles access, peer setup, telemetry and commands; it does not carry
audio. The meeting laptop requires no custom software. Keep this separation.

## Findings and priorities

| Priority | Finding | Improvement and evidence needed |
|---|---|---|
| 1 | GStreamer return stderr was discarded. A running process or advancing ALSA pointer does not prove clean audio. | This change retains return warnings/errors and tuning, including the previous run across a restart. Next expose actual jitter-buffer lost/late counts, decoder recovery and sink discontinuities. |
| 1 | We have no capture immediately before playback. | Add an opt-in, duration-limited capture branch after decode and before gain/dynamics in the **same receiver**, with its own queue and explicit overflow reporting. Compare it with output-side audio and a known source. A second UDP listener on port 5004 is not a reliable copy of the packets; the existing `tools/return-reference-ab.sh` does this and should not be used concurrently with the live player. |
| 2 | USB capture, packet arrival and physical output involve distinct clocks. | Observe the selected GStreamer clock and sink discontinuities before changing the slaving algorithm. If the sink follows another master, compare resampling against skew correction. Merely adding `audioresample` does not establish adaptive clock control. |
| 2 | Buffers exist at multiple stages. | Measure queue occupancy, local relay drops and packet lateness together. The mesh has a 64-packet queue (about 1.28 s at 20 ms/packet), the Pi has a leaky capture queue, and the Mac adds jitter/output queues. Capacities are not constant latency, but queues can accumulate delay or drop audio under load. Do not increase all buffers or shorten them blindly. |
| 2 | Pi power telemetry latched past undervoltage, without active undervoltage in recent samples. | Correlate new power events, ALSA xruns and service restarts with audible timestamps. Verify power/cable stability under simultaneous video and audio load; historical flags alone do not identify the current fault. |
| 3 | Forward voice is processed separately and aggressively. | If the meeting participant also reports bad presenter voice, test that direction independently: ffmpeg pre-gain/limiting, Pi high noise suppression, 6x gain and another compressor. The Mac return defaults cannot repair forward voice. |
| 3 | Historical documentation asserts mutually incompatible fixes and paths. | Treat current code, deployed build identity and measured trials as authoritative. This change labels the old standalone audio guide as historical. Check deployment identity before comparing builds. |

The Pi already has an event-driven host-rate follower with restart guards and an optional
USB pitch controller. Do not add another rate polling/restart loop or enable feedback
control just because the symptom sounds like drift. First verify the deployed gadget mode,
active rate, controller status and actual capture loss.

FEC is enabled and PLC is independently disabled in the app. Keep those choices fixed for
the next comparison. Redundancy is conditional, not guaranteed recovery of every missing
packet; FEC cannot restore audio lost before encoding.

## Next experiment

1. Keep the better gain/dynamics/sync settings and the current buffer constant.
2. Play a known speech/music file through the meeting laptop into the gadget.
3. Capture decoded PCM before the Mac processing/sink while monitoring the same receiver.
   Include elapsed timestamps, packet lost/late counters, capture overflow and playback logs.
4. If the captured PCM is clean but live output is bad, focus on CoreAudio device selection,
   selected clock, synchronization and output-route changes.
5. If PCM is damaged, correlate RTP sequence gaps and timestamps. Capture on the Pi before
   encoding to distinguish USB/capture loss from transport/decode loss. Intact RTP sequence
   numbers alone do not prove the Pi captured all source audio.
6. Recheck using headphones and with video active. Judge repeated speech/music comparisons,
   not only a tone or an average samples-per-second counter.

## References

- [GStreamer audio sink clock algorithms](https://gstreamer.freedesktop.org/documentation/audio/gstaudiobasesink.html)
- [GStreamer jitter-buffer statistics](https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html)
- [GStreamer clock selection](https://gstreamer.freedesktop.org/documentation/application-development/advanced/clocks.html)

These are architectural recommendations, not findings that the remaining fault is proven
to originate at a particular stage. The capture branch and structured counters are future
work; this patch implements defaults, return logging and state visibility.

## Follow-up capture attempts

The user heard the same chipping in decoded PCM captured before Mac output processing.
In a 20-second packet sample, 1,018 Opus packets arrived in sequence with 960-sample RTP
timestamp increments. Arrival gaps of roughly 100 ms recurred every 0.52 seconds. The
receive buffer was reduced to 150 ms for the low-latency trial. Neither packet continuity
nor this short trial establishes the quality of Pi USB capture.

A Pi-side pre-encoder tap could not be activated through `/api/return-tune`. That API
reported success, but fleet readback showed `/etc/default/bridge-return-tune` did not exist,
and no raw RTP packets arrived. The deployed return script hash matched the repository.
The Pi's `/proc/mounts` showed its root mounted read-only. The CLI piped its configuration
into `sudo tee` without checking failure, then restarted the service and exited successfully.
The image build provisions writable binds for peer/agent/network config, but not this tune
file. Thus the control acknowledgement did not mean that the requested pipeline ran.

The repository now propagates tuning write/restart failures and surfaces stderr through
the API. This error-reporting fix is not deployed to the Pi and does not itself make the
tuning path writable. No valid Pi pre-encoder capture was obtained. Both temporary tests
were cleared and normal Mac playback restored at 150 ms. Completing the comparison needs
a writable tuning-path/image fix or a temporary signed capture override. The expected
local script-signing key was absent, and SSH attempts to the known LAN and tailnet addresses
failed; a working Pi administration route is needed for that deployment.
