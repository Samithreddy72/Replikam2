# Forward microphone processing — September 16, 2026

The user reported disturbance while speaking after confirming the separate return
USB stereo fix removed chipping. This change addresses the forward path:
Mac microphone → Opus/RTP → Pi processing → USB microphone → meeting laptop.

## Findings

- The Mac applied +8 dB plus an FFmpeg limiter with automatic makeup gain enabled.
- The Pi forced S16LE, applied WebRTC high noise suppression with AGC disabled,
  then multiplied samples by six before a separate hard-knee compressor.
  Integer saturation at that gain stage cannot be repaired by the later compressor.
- A real GStreamer test on the Pi, using a 1 kHz sine at amplitude 0.3 through
  the old gain/compressor section, produced 62.5% flattened peak samples and
  21.86% harmonic distortion. This isolates a processing defect; it does not
  establish that every noise heard by the user had this cause.
- The saved microphone was SIMGOT EW300 DSP. The running sender used index 1,
  which a fresh device enumeration mapped to the MacBook microphone. Hot-plugging
  can change indices, so this alone cannot identify the previously opened device.
  The user explicitly selected SIMGOT for the restarted session.

## Applied changes

- Sender default gain is 0 dB; the safety limiter retains its 0.9 ceiling with
  automatic makeup gain disabled (`level=false`). The standalone Mac script agrees.
- Pi uses one WebRTC adaptive digital gain controller, compression gain 6 dB,
  target -6 dBFS and limiter enabled. Moderate noise suppression was tested,
  then disabled after the user reported missing pieces of speech.
- Removed the Pi's sixfold integer boost and downstream hard-knee compressor.
- Restarted the source app and selected SIMGOT by name, freshly resolving index 0.
- Installed the Pi script persistently at `/usr/local/bin/bridge-feeder-audio.sh`;
  its original is preserved at
  `/data/diagnostics/bridge-feeder-audio-before-mic-cleanup.sh`.
  Root was returned to read-only. No signed override was present or bypassed.

No jitter-buffer delays were increased. Return playback remains manually pinned
at 0 ms and the proven stereo USB configuration remains enabled (`c_chmask=3`).
The forward jitter/ALSA buffer settings were not changed by this repair.

## Missing syllables: capture and transport follow-up

After the initial gain repair the user reported missing pieces of voice. Turning
off suppression was an isolation step, not a confirmed resolution.

Independent AVFoundation capture of SIMGOT at 48 kHz showed 79 missing 512-sample
blocks in eight seconds (672 buffers observed). A second run with
`-thread_queue_size 512` still showed 82 missing blocks. Rewriting timestamps or
padding silence cannot recover those samples. These measurements are independent
of the meeting application and Pi processing.

The Mac source app now prefers GStreamer's `osxaudiosrc`, resolving the selected
input's CoreAudio unique ID by name through DeviceMonitor. It uses a 40 ms capture
ring with 10 ms reads, Opus 20 ms frames, 64 kbit/s and in-band FEC. The Pi decoder
now enables FEC as well as PLC. This does not increase the configured jitter delay.
Legacy packages without GI discovery retain an explicitly logged FFmpeg fallback;
`/api/state` exposes `voice_backend` so that fallback is visible to diagnostics.
The standalone `mac-stream.sh` still uses FFmpeg and does not get this capture fix.

A ten-second direct CoreAudio test delivered 476,640 samples with zero timestamp
gaps over 1 ms. The actual bundled sender test delivered 484 RTP packets with no
sequence gaps, one legitimate startup pre-skip step, and 482 steps of exactly 960
samples. The live app was restarted and reports `gstreamer-coreaudio` with SIMGOT.
A real conversion/Opus/RTP regression test also passes without a physical mic.

Transport remains a separate unresolved issue: a subsequent 20-second passive
capture at the Pi observed 936 packets, 68 sequence gaps, zero reordering, and a
435 ms maximum arrival pause. The capture socket reported zero kernel drops.
All consecutive-packet timestamp steps were now 960 samples, confirming that the
capture timing defect was removed while transport loss remained. The prior
FFmpeg trial had nonuniform 960/1472/1984-sample steps as well as sequence gaps.
No claim is made that FEC can repair long bursts or that microphone quality is
fully fixed. Listening feedback on the new backend is pending.

The Pi's mesh peer was direct over LAN (~6 ms ping), Wi-Fi power saving was off,
and Wi-Fi reported 5 GHz / -53 dBm. These checks do not locate the packet loss
within the Mac sender, embedded mesh transport, or Wi-Fi delivery.

## Verification and limits

- The replacement full DSP chain processed a 10-second 1 kHz test without
  full-scale clipping. The final second measured 0.011% harmonic distortion.
  Noise suppression and AGC change tone amplitude, so this is a distortion smoke
  test, not a speech-quality or loudness comparison.
- A 20-second passive sample of the existing local RTP reference output received
  3,545 packets / 1,701,814 channel samples, peak -1.50 dBFS, RMS -23.74 dBFS,
  and zero samples at or above magnitude 32,760. This tap is after Pi DSP and
  before the USB/meeting application. No voice recording was retained for this
  measurement. AGC targets are not strict instantaneous peak ceilings.
- Pi feeder active with zero service restarts after installation; sender alive
  using SIMGOT index 0 and the new filter. FFmpeg filter smoke test, shell syntax,
  Python compilation, session-stop (11), and return-playback (7) checks passed.
- The user reported missing pieces of speech with the first AGC/moderate-
  suppression trial. Suppression is now disabled for an isolation test;
  meeting-side listening confirmation remains necessary. These measurements
  cannot verify the meeting application's processing, actual perceived quality,
  or noises already introduced by the microphone itself.

Rollback is the preserved Pi script plus the previous sender gain/filter recipe;
reverting would also restore the demonstrated integer-clipping risk. No SD-card
reflash or reboot is required for this change.
