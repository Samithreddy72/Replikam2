# NetBridge release readiness — 29 September 2026

**HOLD: this branch is not approved for live deployment or flashing.** It contains tested fixes, but the entire historical audit, native distribution and physical acceptance are not complete. No new Pi image, Windows installer or macOS installer has been certified by this work.

## What is actually live

Read-only Fleet observation at 2026-09-28 19:26:52 UTC (29 September 00:56:52 IST):

- NB-002: online/live, image `2.1.0-52a161b`, uptime 2 hours 31 minutes.
- Video, voice and return audio reported active. This does not certify receiver rendering or audible quality.
- Power: `0x50005`; 500 of 500 recent samples reported active undervoltage. Temperature 39.4 °C.
- Reported feeder, camera and return-audio service restart counts: zero. USB-miss measurement was unavailable in this response.
- NB-001: offline. Its last telemetry is not current health evidence.

No live commands, restarts, power changes, image flashes, Fleet deploys or app-channel updates were performed in this continuation.

## Automated evidence

Implementation revision `5e304541e5b556fede56e7af8333ef7571ce631e` passed [CI run 36482356922](https://github.com/Samithreddy72/Replikam2/actions/runs/36482356922): 1717 common checks, zero failures/skips/missing results; Linux media 11; Mac encoder 9; desktop unit 3 plus build; nine browser checks across Fleet, Source and Studio; actual Caddy configuration validation; and native Go race tests on Mac, Windows and Linux. These are software checks, not physical or native installer certification.

Targeted checks cover signed update tampering and recovery, wrong-product OS manifests, sleeping-laptop guards, fresh/complete idle evidence, bounded HTTP bodies and worker threads, DNS-rebinding refusal, CLI target selection, stale/malformed telemetry, and preserved PIN/audio controls. See WORKFLOW-IMPLEMENTATION.md for exact checkpoints. AUDIT-VERIFICATION-STATUS.json conservatively tracks all 93 high/medium historical findings after reviewer severity/refutation; an open entry is not silently marked fixed by a passing unrelated suite.

## Material release holds

1. Finish reconciling and testing the remaining audit findings. Sixteen historical findings remain explicitly open, with additional partial/mitigated items. Signed script identity/revision binding is now implemented; whole-OS anti-rollback, old-slot owner-key revocation migration, persistent image machine identity and early trial-boot recovery still require work. Existing tests do not close these by implication.
2. Finish the agreed workflow scope: stable presenter identity and revocation, complete backup scheduling/off-device restore acceptance, fallback behavior and remaining UI parity. Wi-Fi and phone work stay excluded.
3. Build actual candidate artifacts on their native platforms. Test installation, start, capture permissions, helper operation, quit/reopen, whole-package upgrade and rollback on Windows and macOS. Code signing/notarization credentials are not configured here; do not label developer/ad-hoc builds as trusted signed distribution.
4. Boot the Pi candidate from a spare card and run the full receiver test with the real USB meeting laptop. Preserve the tested WAN/audio profile. Never restart the gadget with the laptop attached.
5. Resolve and qualify the electrical power path. The current 100% sample warning cannot be cleared by a software audit. Follow POWER-ACCEPTANCE.md; keep audio buffering out of the electrical fix.

The standalone Source updater now refuses an incompatible helper and refuses binary-only swaps inside `.app` bundles. Those cases require a complete installer. Successful-launch-then-crash rollback is still outstanding; failed rename/exec restoration is tested.

GitHub Actions artifact storage is currently full. Existing image draft releases retain their old assets; nothing was deleted. Test summaries remain in run logs, and optional evidence upload failure does not override the actual test verdict. Verify each candidate artifact's delivery path before starting a release build.

## Promotion order

One canonical Pi candidate, one Fleet revision, and one shared Studio source revision producing native Mac/Windows packages should form a recorded compatibility set. First complete software gates and preserve checksums/signatures, then qualify one spare-card pilot, then review the exact artifact set and rollback evidence before live promotion. Until then, keep the working setup on its existing versions.

## Latest power comparison — GPIO jumper supply

The owner confirmed GPIO-header power through jumper wires. Connected/live baseline at 20:55:54–20:56:22 UTC: active undervoltage in 6/6 fresh readings. The owner then physically unplugged the client and ended the presenter session. Four distinct Fleet heartbeats at 20:59:02–20:59:50 UTC showed active undervoltage in 3/4 readings, temperatures 38.5–38.9 °C and no monitored service restarts. The fault therefore persists at idle on GPIO power. Media load changed too, so this does not isolate USB's contribution. The retained `configured` USB status is a known stale-reporting issue confirmed by the owner; it is not evidence the cable remained attached. Supply rating, jumper/contact quality and reverse-current isolation still need physical verification.
