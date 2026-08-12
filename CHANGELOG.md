# Changelog

Verified restore points and released builds, newest first. Each `netbridge-*` tag is a
state that was tested against real hardware; each `app-v*` tag is a published presenter
build on the [Releases](https://github.com/Samithreddy72/Replikam2/releases) page.

## netbridge-MULTIRATE-VERIFIED-2026-08-13
*2026-08-13 · `6ed972e`*

**Restore point.** First state that is both multi-rate and audibly clean on a card flashed
from a published image — no signed override, no card surgery. `c_srate = 48000,44100,32000`
verified on the running gadget; 48k → 32k → 44.1k → 48k followed live mid-meeting with zero
errors and zero restarts. Full write-up in `docs/evidence/multirate-verified-2026-08-13.md`.

## Unreleased — jitter, diagnosis and baseline
*2026-08-13*

**The buffer that matters was on the wrong machine.** Room audio is decoded on the
presenter's laptop, so every jitter action the fleet had ever offered tuned the direction the
operator was not listening to. The bridge now publishes desired tuning and the app adopts it
on its existing 10-second poll — a fleet click that changes the presenter's audio mid-call
with no video interruption.

- **Diagnose** names the culprit from measured evidence and offers only the treating action.
  Thirteen culprits, all previously observed here; five of them honestly offer **no** action.
- **Jitter ladder** — rungs 1 and 2 raise the presenter's buffer without touching video;
  rung 3 sits under "Interrupts the stream".
- **Golden Profile** — save a known-good config, see drift, restore the tunable parts.
  `c_srate` and script hashes are reported as NEEDS DEPLOY, never falsely "restored".
- **jitter-sentry acts while live**, using only the video-safe rung, and can never overwrite
  a rung an operator set by hand.
- **Power readout fixed** — `bridge-web` runs as `pi` and cannot open `/dev/vcio_gencmd`, so
  vcgencmd's error text was being stored as the reading. Now reads the mask from the flight
  recorder, which root already writes every second.
- **Menu reorganised by symptom**, with every interrupting action stating its cost.

Tests: 50 → 156. `test-gst-pipelines.sh` rewritten — it no longer greps source, it runs each
real script with a stub `gst-launch-1.0` and builds the resulting argv.

## app-v1.1.4
*2026-08-11 · `9d86725`*

NetBridge Source app-v1.1.4

## app-v1.1.3
*2026-08-11 · `2dee37c`*

NetBridge Source app-v1.1.3

## netbridge-POWER-TRUTH-2026-08-11
*2026-08-11 · `50dfc6c`*

Restore point: under-voltage made visible, brownout no longer mis-repaired

## netbridge-APP-VERIFIED-2026-08-11
*2026-08-11 · `0d070d4`*

Verified restore point: presenter app working on macOS, Windows built

## netbridge-VERIFIED-2026-08-10
*2026-08-10 · `739fb1d`*

Return audio verified CLEAN BY EAR at 48k / 44.1k / 32k on hardware.

## netbridge-AUDIO-CLEAN-2026-08-08
*2026-08-08 · `86bbafe`*

Return audio verified clean by ear on hardware.

## netbridge-PHASE6-VERIFIED-2026-08-04
*2026-08-04 · `1942c49`*

Phase 6 verified on hardware, second run — plus the night's fixes

## netbridge-PHASE6-COMPLETE-2026-08-01
*2026-08-03 · `70f3f82`*

Phase 6 acceptance passed: 32/44.1/48 kHz live rate-following, clean by ear at every rate, zero interventions. Script md5 on card: 0c7bf8fa (watchdog-10s update pending next deploy).

## netbridge-WORKING-2026-08-01
*2026-08-01 · `05fae76`*

VERIFIED WORKING — all five checks green, audio confirmed clear by ear

## netbridge-restore-2026-07-31
*2026-07-31 · `e2634a6`*

Restore point — 2026-07-31 (pre phase-6)

## netbridge-restore-2026-07-29
*2026-07-29 · `d1623a3`*

Restore point — 2026-07-29

## app-v1.0.5
*2026-07-29 · `d1623a3`*

control-plane: wire the fleet rollout panel to /admin/rollouts

