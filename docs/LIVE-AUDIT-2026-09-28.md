# NetBridge live audit — 28 September 2026

Work branch: `codex/netbridge-live-audit`, based on `fleet-fixes-merged` at `32a2d6c`.
These changes are local and are not deployed. The live meeting was observed through read-only status/log requests; no media pipeline was restarted or reconfigured.

## Fixes

- A GET of presenter `/api/checks` cannot create or replace a mesh helper. It reuses only the active route for the selected bridge, and rejects cross-site browser requests. Checks before a mesh exists return an explicit unavailable result; starting a session remains an explicit action.
- Failed or unavailable status requests and a locked PIN session clear stale green readiness indicators without stopping the media session.
- A factory/bootstrap token can enroll a new device, but cannot rotate an existing device's credential. Existing-device enrollment requires its current bearer token and the bootstrap token's organization must match the device's organization. This covers unclaimed as well as claimed records.
- `set-peer` compares parsed whole IP/port fields, rather than matching a prefix such as `.3` inside `.34`. Exact and quoted matches still avoid a restart. No DSP/codec/buffer settings change.
- Power diagnostics report observations and possible consequences, without promising that a larger return buffer fixes electrical under-voltage or asserting that every audible artifact is electrical.
- Drift checks refuse to call ahead/divergent or dirty deployed builds “in sync.” The bridge diff classifier ignores file headers as well as comment lines.
- The test gate fails on a nonzero process exit even with a success-looking footer. unittest skips are counted and block a release-green result. `--media-python` allows GStreamer integration to use its installed Python ABI separately from the backend runtime.
- The audio rate-following test stops only its own fixture processes; broad process-name kills were removed.
- Two stale tests were corrected: process shutdown is tested behaviorally, and historical drift is checked per security section instead of assuming no later security commits exist. The PIN-flow mesh fixture now records bridge identity, matching the real helper.

## Enrollment deployment requirement

A reflashed/replaced card that has lost its device credential will no longer regain an existing identity solely from the shared factory token. This is intentional: that old path is also the takeover path.

Before deployment, retain an owner-controlled identity recovery procedure. With the bridge idle, back up its organization/label/configuration and deliberately retire/reprovision the old identity using the owner's authenticated administration workflow; do not silently overwrite it, expose its pending provision data, or reset a live meeting's identity. Existing bridges with valid tokens continue telemetry/provision normally. New device IDs enroll normally. Any automated recovery design must use a device-specific credential or an owner-issued, scoped, expiring recovery grant. That recovery grant feature is not implemented here.

## Test runtimes

The backend suite requires its supported Python environment and dependencies from `control-plane/backend/requirements.txt`, plus `httpx`. USB audio analysis requires numpy. Real audio integration requires PyGObject/GStreamer with Opus plugins; it uses synthetic sources and a fake sink, never a live microphone/speaker.

```sh
FLEET_TEST_PY=/path/to/backend/python \
  bash tools/run-tests.sh --python /path/to/backend/python \
  --media-python /path/to/gstreamer/python
```

The exact local test command, full logs, live samples, and original-code regression checks are in the companion `netbridge-analysis/live-audit` directory in this Codex workspace. Five new regression groups were run against a temporary copy with original source restored; all completed and failed as expected. Deployment requires separately built and verified app/image artifacts; source tests do not establish live hardware readiness.

## Validation

The combined suite completed with **1,619 passed, 0 failed, 0 skipped, 0 without a result**. This includes six synthetic GStreamer integration tests. The subsequently added UI freshness regression passed separately (one test), and the complete embedded JavaScript passed Node syntax validation. Five regression groups failed against the original code as expected. These are software checks, not receiver-side hardware acceptance.

## Scope limits

Read-only observations can show frame-pump throughput, process continuity, PCM progress, USB errors and mesh counters. They cannot establish the receiving laptop's rendered pixels, subjective audio, or camera-to-display latency without receiver-side capture. No live fault injection, reboot, lock, update, disconnect, packet flood or power manipulation was performed. Cross-country behavior and real Windows client behavior remain distinct acceptance tests.
