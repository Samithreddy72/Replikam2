# NetBridge implementation and verification ledger

Requested 2026-09-28: complete Claude + Codex workflow suggestions, excluding Wi-Fi setup/preload changes and phone-specific viewing/installability. No live deployment or media disruption is part of local implementation.

## Baseline

Branch codex/netbridge-live-audit; audit fixes dbdcd2d. Prior suite: 1619 passed plus separate UI regression. Historical specifications describe older code: completion must be demonstrated against this checkout, not inferred from their prose.

## Current checkpoint (29 September)

This is an unreleased implementation in progress. The handoff bundle resolves to 6504251, which adds documentation to 32a2d6c; it does not supersede the audit fixes in dbdcd2d. Wi-Fi and phone work remain excluded. No fleet deployment, image flash or app-channel publication has occurred.

Verified locally so far: queue guard/scheduling/cancellation/expiry and tenant boundaries (7 HTTP tests); Pi last-moment guard (2); presenter setup/report safety (3); digest retries and mesh expiry parsing (3); backup copy/restore (1); pilot USB serial validation (2); Source/Studio/Fleet browser flows (3); native macOS tray compilation (1 Rust test). Updater tests now cover real signatures, staging tampering, launch-failure restoration, bundled verification and protocol/platform compatibility (7).

The full checkpoint before the latest image/updater edits was 1629 passed, 1 failed, 3 skipped. The confirmation contract failure is fixed and its 24 checks pass. Three GStreamer pipeline checks require webrtcdsp/webrtcechoprobe unavailable in this Mac installation. They remain visible release blockers until run in the configured Linux CI environment. A later full run is in progress; do not treat these figures as its verdict.

## Scope

| Item | Work | Verification |
|---|---|---|
| H1-meeting-guard | Implemented at queue, delivery and new agent execution; confirmed override audited | HTTP + agent tests pass; deployed old agent unchanged |
| H2-maintenance-mode | Timed window, reason, notification mute, holds idle jobs | HTTP/browser coverage; broader alert acceptance remains |
| H3-grouped-email-digest | Opt-in daily digest with recipient-level partial retry; existing grouping retained | Mock SMTP tests pass; real delivery not sent |
| H4-mesh-key-expiry | Optional read-only inventory and fresh expiry warnings | Malformed/stale/disabled expiry tests pass; credential not configured |
| H5-fleet-selfcheck-backups | Basic self-check and consistent private SQLite backup tool | Restore test passes; nightly/off-device workflow unfinished |
| H6-stable-presenter-identity | Not implemented; needs registration, revocation, clone handling and safe client migration | Outstanding |
| M1-ack-snooze | Episode-scoped acknowledgment and bounded snooze | HTTP tests pass |
| M2-free-space-command | Confirmed archived-journal cleanup to 64 MB | Contract passes; physical command not executed |
| M3-run-when-idle | Deadline-bound waiting queue and cancellation | Busy/freshness/expiry/delivery tests pass |
| M4-unique-usb-serial | Opt-in CPU-serial pilot; legacy identity unchanged by default | Function tests pass; physical Windows/Mac pilot outstanding |
| M6-build-attestations | Not implemented; must stay dormant on current repository plan | Outstanding |
| M7-tests-on-main-merge | Strict CI test gate wired into build workflows | Local runner checked; remote CI not yet run; main untouched |
| P2-readiness-check | Fleet checks with unknown evidence explicitly represented | HTTP coverage; not receiver certification |
| P7-sites-notes | Bounded labels/notes searchable; maintenance dialog | HTTP and browser coverage |
| P4-bulk-actions | Scoped API plus idle-only reboot/restart selector | Other planned bulk UI actions outstanding |
| P6-session-history | Telemetry-observed intervals, gaps labelled unknown | HTTP coverage; presenter attribution outstanding |
| P3-health-check | Read-only structured checks | HTTP/browser coverage |
| P1-live-now-strip | Reported active/USB/power summary | Presenter name/duration/full metrics incomplete |
| P5-timeline | Paged command/alert/report/session timeline | Cursor test passes; audit/update/restart events incomplete |
| Preflight and OS-specific permissions | Reconcile / complete | Pending |
| Linked problem report | Reconcile / complete | Pending |
| Remember bridge and PIN-based start | Reconcile / complete | Pending |
| Compatible idle-only app updates | Reconcile / complete | Pending |
| Live/recovery/relock status | Reconcile / complete | Pending |
| Honest quality indicator | Reconcile / complete | Pending |
| Tray/menu bar | Reconcile / complete | Pending |
| Shared status semantics across Source/Studio/Fleet | Reconcile / complete | Pending |
| Guided inspection and repair | Reconcile / complete | Pending |
| Owner-controlled identity recovery | Reconcile / complete | Pending |
| Native signed distribution readiness | Reconcile / complete | Pending |
| Device labels/search/keyboard accessibility | Reconcile / complete | Pending |
| Compatibility matrix and pilot release validation | Reconcile / complete | Pending |

## Release constraints

- Preserve the working media configuration and existing Wi-Fi setup. No audio DSP changes.
- Main represents deployed work; no main update, push, release publication, flash or live command here.
- Unique USB serial: pilot first on one bridge and real Mac/Windows receivers.
- Attestations: dormant until supported; existing artifact signatures remain required.
- Native release signing needs owner credentials; never substitute ad-hoc signatures for trusted distribution.
- Physical Pi boot, receiver rendering, real Windows capture and external backup delivery require their actual environments; local mocks cannot certify them.

## Additional handoff corrections in progress

- B60: image builder gives the owner a login shell while keeping the password locked; both image checkers reject an unusable shell. Actual rebuilt-image verification pending.
- B61: three media units use a writable systemd cache directory for the GStreamer registry. Pi startup-time measurement pending.
- B62: image preflight and DKMS failures now stop the build; the read-only-write guard no longer uses an unset ROOT. Negative-control test passes.
- B80: OTA mountpoint defaults to /run; failure to create it stops before formatting. Existing OTA suite is being rerun.
- Presenter updater: signed metadata reverified before swap, safe filenames, old executable restoration on failed rename/exec, clean PyInstaller relaunch environment, bundled cryptography verifier, isolated /app/pin-v2 channel and bridge compatibility check. Helper sidecar update and post-start health rollback remain unfinished; real Windows update acceptance remains required.
- Launcher now validates status JSON and reports port conflicts. Power trim frequency log corrected; electrical acceptance is documented in POWER-ACCEPTANCE.md.

The historical backlog is not closed wholesale. Fallbacks, persistent presenter identity, full off-device backup automation, dormant attestations, complete linked incident logs, full UI parity, native signed distribution and physical acceptance still need work. No claim of full release readiness is justified at this checkpoint.

### Verification update

The later Mac regression checkpoint completed with 1645 passed, 0 failed, 3 skipped. Subsequent focused checks: updater 7, presenter PIN/peer-confirmation 66, signed overrides 54, structured agent results 3, staging trust 1. These are overlapping targeted checks, not an additive claim about a single full run.

Linux CI run 36466933139 failed: 1596 passed, 44 failed, 5 skipped. Identified harness gaps include shallow Git history (historical drift fixtures), BSD-only file-mode/size commands and missing media dependencies; remaining failures require individual logs. The runner now preserves per-test evidence and prints failed output. A Mac-specific VideoToolbox test still needs explicit platform coverage in the CI design. No release is approved by this result.

Additional source fixes: require positive bridge peer confirmation before go-live/resume; keep diagnosis JSON complete or report a failure; busy override mounts may be lazily detached under the existing restart policy, and failed detach cannot claim rollback; image staging uses the pinned owner key, validates release identity and no longer hardcodes version 2.0.0.

### Continued CI audit (29 September)

Run 36467985733: 1617 passed, 32 failed, 4 skipped. Owner-key-dependent signer tests exposed a real `--pubkey` false-success bug; signer now propagates failures and all 25 override/signing checks pass using disposable keys. Status-cost test now models a settled Pi with live feeder PIDs (14 passed), rather than depending on CI host uptime. OTA test tracing added to diagnose Linux-only failures; isolated Mac run passes 39 checks.

CI separates required common, Linux media and Mac encoder jobs. Ubuntu Noble disabled webrtcdsp ([distribution changelog](https://lists.ubuntu.com/archives/noble-changes/2024-February/009799.html)); the Linux media job uses Jammy's real plugin. Mac encoder gate locally passes 9 checks with real generated-video encoding. No skipped test is relabelled as passed. GitHub artifact storage quota is exhausted: run logs remain readable, but uploading test evidence/build artifacts is blocked. Existing release assets have not been deleted.


Run 36469283397: Linux media and Mac encoder jobs passed. Common suite: 1617 passed, 29 failed, zero skipped. All failures were in the publishing test; its expanded heredoc contained an unescaped parameter expression in a comment. Bash 5 nounset aborted writing the mock SSH executable, which was then empty and returned false success. Removed the expansion and added a positive fixture execution check. Mac publishing regression: 39 passed. Linux rerun is required before closing it.

Additional safeguards: disruptive work requires explicit boolean video/voice state (9 workflow HTTP tests and 2 device guard tests); update feed generation now matches /app/pin-v2 and binds required helper bytes (2 packaging tests). Updater refuses a mismatched/missing helper and refuses binary replacement inside native .app bundles (8 update tests). Whole-bundle updates and helper upgrades still require a complete installer; no claim of native automatic-update completion. Optional inventory token is now passed through Compose; not configured live.

Artifact retention is best-effort, matching the image workflow: a quota error is recorded but cannot overwrite the actual mandatory test verdict. No existing artifact was deleted. Draft release/build delivery still requires checking each workflow's upload path.

Further targeted audit fixes: B171 sleeping USB hosts now block both OS updates and pending camera overrides (44 OS-update checks; 57 signed-override checks). B88 product binding now rejects signed disk/wrong/absent product manifests before formatting; general anti-rollback policy remains open. B338 deployment template now names mesh settings and explains exact tag matching; admin bootstrap and device bootstrap documentation corrected. CLI B306/B310/B313: missing fleet numbers cannot fall back to another device, duplicate names require disambiguation, weeks count toward uptime, and transport errors fail clearly without automatically retrying writes (4 focused checks).

The local common checkpoint had 1554 passing assertions and 3 crashed test files, not a green verdict. All three used incomplete idle telemetry fixtures now intentionally refused by the guard; corrected fixtures pass 43 + 12 + 38 focused checks. A complete updated CI run is still required.

Run 36470195422 confirms the Linux OTA publishing regression passes all 39 checks. Its only failures were the three incomplete-idle fixtures, subsequently corrected. CLI telemetry/error handling now has 8 checks: malformed nested status, offline honesty, early legacy-PIN warning, missing/ambiguous targets, week uptime, write transport failures and interrupted HTTP error bodies. The conservative per-finding ledger is AUDIT-VERIFICATION-STATUS.json; findings not reconciled to current code remain open.


Checkpoint f873976 passed every required CI job in run 36470772365. Subsequent security audit changes require another gate: B6 Fleet pre-parse body limits/timeouts (6 boundary tests plus 9 workflow HTTP tests); B26 bounded Pi HTTP workers/body size/socket timeout (2 real-socket tests, 31 PIN checks); B112 loopback Host validation on all app GET/POST paths (4 read-only/security tests, 66 Mac/Windows PIN-flow checks). CI now validates the Caddyfile using its actual container image. No live proxy, Pi or presenter process changed.

Fresh read-only live check (2026-09-28 19:26:52 UTC): NB-002 remains live on 2.1.0-52a161b, power 0x50005 and 500/500 recent undervoltage samples; monitored restart counts zero. No receiver pixels/audio were available. Fleet candidate now replaces legacy audio-buffer power advice with evidence-based maintenance guidance without rewriting stored telemetry, and rollouts share the same complete/fresh meeting guard as direct commands (10 HTTP workflow checks, 60 alert checks, 80 rollout checks). RELEASE-READINESS.md records the remaining holds; no deployment or hardware qualification is claimed.


HTTP checkpoint dfde734 passed all CI jobs in run 36472001929: common 1670, Linux media 11, Mac encoder 9, desktop unit 3, desktop build and actual Caddy configuration validation. Presenter audit B106/B110 subsequently fixed: shared sign-out capture cleanup and no automatic retry of an ambiguous PIN POST. Two new boundary tests, 66 PIN-flow checks and 5 desktop boundary tests cover this change; these require the next full gate.


## 29 September — mesh control browser boundary

Checkpoint 38980c0 passed full CI: common 1673, Linux media 11, Mac encoder 9, desktop 3 plus build and Caddy validation. Logs are retained locally.

B111: the helper previously forwarded arbitrary TCP from loopback to the bridge. It now parses HTTP, requires the exact local Host, rejects browser Origin/Referer/Fetch Metadata and upgrades, permits only native GET/JSON POST, bounds request bytes and deadlines, and dials only its configured bridge. Transport keep-alive is disabled so ambiguous PIN writes are not replayed. Tests cover hostile browser requests, legitimate responses and lost-response behavior; required Go race tests run on Mac, Windows and Linux. Full installer and live mesh acceptance remain separate. No live restart or deployment occurred.

Implementation references: [Go ReverseProxy](https://pkg.go.dev/net/http/httputil#ReverseProxy) and [HTTP Server timeouts](https://pkg.go.dev/net/http#Server).


## 29 September — honest cancellation and provisioning failures

B257: Fleet baseline Cancel now closes without a command; saving unconfirmed has a separate explicit button. Two local Chromium workflow tests pass, including no writes on Cancel; both are required in CI. B333: provisioning remains after command delivery and now tolerates socket timeout/reset, incomplete HTTP response and invalid JSON, and ignores malformed payloads. Three new isolated tests pass. No live provisioning or baseline command was issued. B20 was reconciled against its existing passing real-Git-history test.


## Presenter status reconciliation

Source retains the engine's active bridge across page reloads, keeps lockout messages through polling, separates connection path from audio warnings, uses green readiness styling, and hides advanced diagnostics until a live session. All eight Source/Studio/Fleet Chromium checks pass locally, including three new Source regressions; all browser files are now required in CI. Existing PIN/peer tests are rerun unchanged. B181 and the B145–147 test/environment findings were reconciled against successful eb8cc7d evidence. Release and physical acceptance remain open.


## Update guard corrections

B84/B85: per-content proof prevents a later venue outage from reverting an update that already reached Fleet. Unproven trials use boot ID/uptime; no RTC/NTP assumptions. B91: deliberate starts reset the crash sequence using systemd NRestarts; real automatic restart sequences still quarantine. B170: active state and restart evidence are required; completed oneshots need success/zero exit status. B90's existing pending-restart preservation now has a direct revert-all/live-session regression. These are sandboxed signed-script tests, not a physical update qualification. Reference: [systemd v257 service start and restart accounting](https://github.com/systemd/systemd/blob/v257/src/core/service.c).


## Signed context and CLI sequencing

New images require `script-signature-v2` and the read-only `bridge-verify-update.py`. The publisher signs product/name/monotonic revision inside a comment; original payload bytes are otherwise preserved. Installer, loader, bind verification and quarantine restore reject mismatched names or revisions below a persistent acceptance floor; a same-revision different body is refused. Revert-all does not remove the floor. Existing legacy signatures remain usable only on legacy-policy images. Before flashing the new candidate, retained legacy overrides must be reviewed and re-signed; never silently discard a working audio override. This is not whole-OS anti-rollback. Local evidence: 5 real-signature context tests, 74 signed-update checks, 26 loader checks, 17 publisher checks.

CLI now confirms before publishing, distinguishes explicit paths from bare catalog names, and queues deploy/revert for idle with a 30-minute expiry without stalling on offline bridges. Twelve CLI tests pass. A queue acceptance is explicitly not execution success.

CI 36478934149 for 6b0ebe8 passed 1687 common checks but failed the extracted-JavaScript freshness harness because its copied helper list omitted the new tone function. The harness now includes the actual new function and lockout state; its behavior test passes locally. Full CI remains required for this batch.

### Owner SSH revocation (29 September continuation)

The strict image policy now uses an AuthorizedKeysCommand backed by the durable accepted signed owner-key snapshot. Static AuthorizedKeysFile lookup is disabled. Signature verification and revision checks occur on each new authentication; missing snapshots and corrupt acceptance records fail closed. Seven real-signature tests and 23 SSH tests pass locally, including real sshd configuration validation. Both old OS slots and existing authenticated sessions require separate migration handling; B87 remains partial. This does not change the live bridge.
