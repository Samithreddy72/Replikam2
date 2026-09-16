# Return audio timing replay — 2026-09-16

Input: opt-in capture `cff81fd7d43b4883be935c3ef7524759`, SHA256
`b363e76cca3e8b18c48cbaca93d2887252ddd7ff87721fffc89e1e9248292d23`.
755 consecutive RTP packets, every timestamp step 960 samples (20 ms at 48 kHz).
No missing sequence numbers. Maximum recorded arrival pause: 429 ms.

The same captured packet payloads and arrival offsets were replayed through the source
receiver into a synchronized fakesink. Replay scheduling error was at most about 6.4 ms.
A timestamp gap is next PCM PTS minus previous PCM PTS minus previous PCM duration.

| Offline receiver | PCM timestamp gaps >1 ms | Largest absolute gap | Lost / late packets |
| --- | ---: | ---: | ---: |
| Default slave clock mode, 100 ms buffer | 428 | 235.7 ms | 0 / 0 |
| Default slave clock mode, 600 ms buffer | 43 | 13.7 ms | 0 / 0 |
| No skew adjustment, 0 ms buffer | 0 | 0 ms | 0 / 0 |

All three replay WAVs contained **byte-identical decoded PCM**. This isolates the
change to scheduling timestamps rather than the decoder's sample values. It does not
establish that the PCM is free of audible artifacts: signal damage upstream could be
present in every replay. Nor does fakesink demonstrate uninterrupted device playback;
late buffers, real output underruns and long-term source/device clock drift need testing.
In particular, the long arrival pauses cannot be made to disappear by changing timestamps.

The no-skew experiment used `rtpjitterbuffer mode=none` before packet replay.
It is now reproducible with `tools/evaluate-audio.py --clock-mode none --jitter-ms 0`.
Default `--clock-mode slave` preserves the production receiver's mode. No live receiver
clock-mode change was made. The live receive-buffer trial remains 0 ms.
NetEq was not run. No new QuickTime recording was available during this analysis.

Local results: `/tmp/netbridge-chip-replay-100`, `/tmp/netbridge-chip-replay-600`,
`/tmp/netbridge-chip-replay-clock-none`. These contain private audio and are not committed.

Next validation: a bounded actual-output A/B of clock modes, capturing sink warnings,
render timing and user assessment, followed by a longer drift/underrun test before making
any default change. Pi pre-encode capture is still needed if chipping remains in the PCM.
