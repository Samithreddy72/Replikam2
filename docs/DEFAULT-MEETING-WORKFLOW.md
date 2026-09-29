# Default meeting workflow and release qualification

## Presenter
Select the bridge and remembered devices, enter its current PIN, then choose Go live. Invalid PINs must not start camera or microphone capture. Starting a session requires confirmation from the bridge. Relaunching the app restores choices, not an active camera or microphone.

Studio shows four checks: video arriving at the bridge, voice arriving, the meeting laptop's USB camera connection, and return audio. Missing or stale evidence must never count as passing. USB configured is connection evidence only; it does not prove Teams is displaying the picture. Final meeting picture and sound require a client-side check.

Video uses the existing 424×240 at 30 fps profile. The receiver publishes complete frames directly to the independent UVC sender. Temporary video interruption holds the last clean frame for up to 60 seconds, then black. Stop, expiry, lock and replacement clear the previous session immediately. Recovery must not replay buffered footage or reset working audio and USB.

## Fleet admin
1. Select the affected bridge and run Diagnose. Check freshness and the reported failing direction.
2. Use Recover video for a video-only fault. Broad restarts and tuning remain under advanced actions; do not use them as the first response during a meeting.
3. For updates, select a qualified signed catalog release. Use one available bridge as a canary, scheduled for idle maintenance, before expanding to site groups.
4. Signed script files persist under /data/overrides. Stored or waiting is not the same as applied. Full OS updates write the inactive slot and remain provisional until trial-boot health commits that slot.
5. Confirm final version, fresh health and command outcome. A rollback or failure must halt expansion. Keep the previous qualified release available.
6. Save a known-good baseline only after the actual meeting workflow is verified. Use drift reports and support diagnostics for subsequent differences.

## Permanent startup policy
The production image masks obsolete loopback/test-pattern producers, optional diagnostic redraw/pitch services, and stock SSH listeners. The authenticated bridge SSH service remains available. Safe-mode quarantine considers only updates present when the failed-boot recovery began, not a repair installed afterward.

## Release gates
Automated PIN, session, stop, helper, fallback, rollout, signature and UI tests are necessary but not hardware qualification. Inspect the signed built image and compare its contents to the exact revision. Verify native installer contents. Then perform a maintenance cold boot, a correct/incorrect PIN check, two start/stop sessions, video interruption/recovery, USB reconnect and meeting-side audio/video checks. Do not replace a working live session to perform this audit.

Windows may retain a generic camera friendly name while the bus reports Logi. Refresh the device entry only outside a meeting; do not change USB identity merely to force a new name.
