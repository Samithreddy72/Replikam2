# NetBridge Studio

Tauri 2 desktop application with the approved light/dark Studio design. The UI
is local React/TypeScript; a private authenticated Python engine owns the existing
FFmpeg, GStreamer and mesh processes. No customer shell scripts or browser tabs.

## Included

- Native window, single-instance handling, and graceful engine shutdown.
- Fleet sign-in and invitation codes, bridge selection, device choices, PIN unlock,
  camera transmission, outgoing microphone mute, and return-audio toggle.
- Local camera/microphone preview and system-speaker test. Preview capture stops
  before Go live so the engine owns the camera. Live video is not duplicated into
  the UI; bridge-side delivery measurements are shown separately.
- Audio recovery and tuning, an expandable diagnostic drawer, and minimal support
  report export to Downloads (no account identifiers, credentials, or recordings).
- Local completed-session history and a Cmd/Ctrl+K navigation palette.
- Camera-off sessions send plain black video without opening the camera. Camera
  and microphone mute controls work independently before and during a session.
- Light, Dark and system appearance, an animated launch and connection flow, and
  an optional local display name. Personas are not exposed in Studio navigation.
  Screen transmission and arbitrary speaker selection are not implemented.
- Signed whole-app updater integration. Installation refuses active or desired
  sessions, and serializes with Go live. No endpoint/key is configured in dev builds.

The meeting-room laptop still installs nothing. Studio runs on the presenter Mac or Windows PC.
The existing source application and its browser interface remain available.

## Development

Requires Node 22.14+, Rust 1.88+, Xcode command-line tools on macOS (or the Tauri
Windows prerequisites), the source app Python environment, and verified media
binaries in `../netbridge-source/_bundle/runtime`.

```bash
npm ci
npm run desktop
```

`NB_PYTHON` selects a different development Python. `npm run dev` opens a browser
**UI preview only**, with no engine bridge. The default route shows the signed-out
entry screen. For a clearly labelled, development-only interactive fixture, open
`http://127.0.0.1:1420/?preview=studio` or `?preview=signin`. Sample sign-in code
and bridge PIN: `123456`. These routes do not contact Fleet or start media, and
the fixture is excluded from production builds.

Design review screens: `?preview=splash`, `?preview=signin`, `?preview=name`,
`?preview=connecting`, `?preview=welcome`, and `?preview=studio`. Add `&theme=light`
or `&theme=dark`, or use the appearance switch. The screen picker is development-only.
Connecting/splash/welcome routes stay on that stage for visual review; ordinary sign-in
loads real workspace data once and proceeds when ready, with retry on errors.

Appearance follows the OS by default and can be changed to Light or Dark. Animations
respect reduced-motion preferences. A first sign-in asks for an optional display name,
stored locally per account and Fleet URL; Settings can change it. It is not a Fleet account
rename, and it does not affect authentication. Existing live controls remain accessible
without completing name onboarding. The design was approved for the 0.1.11 release.


## Build a self-contained app

```bash
npm run package -- --bundles app        # macOS .app
npm run package -- --bundles dmg        # macOS installer image
# On Windows: npm run package -- --bundles nsis
```

The packaging script freezes Python, copies the complete verified media runtime
verbatim, compiles the mesh helper from the checked-in Go source, and puts everything inside the app.
Go is required on the build machine; `NB_GO` can select its executable.
`NB_MEDIA_DIR` and `NB_PYTHON` override the build inputs. Windows must be built on
Windows using a Windows media runtime; it has not been validated on this Mac.
The standard build uses legacy GStreamer playback if GI is absent. To package the
persistent GI audio engine, use a matching GI-enabled Python/runtime with
`NB_PERSISTENT_AUDIO=1`; validate that build on clean hardware before release.

Output: `src-tauri/target/release/bundle/macos/NetBridge Studio.app`.

A local developer build is **not a notarized customer release**. Configure Tauri's
Apple signing/notarization environment (`APPLE_SIGNING_IDENTITY`, Apple credentials)
and verify all nested Python/media binaries before distributing. The app has camera,
microphone, and local-network usage descriptions and media entitlements.

Without an Apple signing identity, packaging uses an ad-hoc bundle signature, and
CI verifies bundle integrity before collecting artifacts. For a trusted test build,
try opening the installed app once, then use **System Settings → Privacy & Security
→ Open Anyway** if macOS offers the override. Managed Macs may prohibit this.
Ad-hoc signing does not verify the publisher or provide notarization. A “damaged”
message should be investigated as a packaging/integrity failure, not treated as the
normal developer warning; use a corrected build instead.

## Signed updates

Set `NB_UPDATE_ENDPOINT` (HTTPS manifest URL), `NB_UPDATE_PUBLIC_KEY` (Tauri public
key contents), and `TAURI_SIGNING_PRIVATE_KEY` in the release environment. The
packager adds the updater configuration and creates signed updater artifacts.
Never commit the private key. Publish the complete artifacts and platform manifest
to the chosen endpoint. Customers use **Settings → Check for updates → Install and
restart**. This does not publish anything automatically. Staged rollouts and automatic
background scheduling require release-server policy and are not enabled here.

Legacy executable-only updates are disabled in the desktop engine, because replacing
one executable would leave the app signature and bundled helpers inconsistent.

## Validation

```bash
npm run build
npm test
npm run test:ui                      # Playwright; install Chromium first
cd src-tauri && cargo test
```

The UI integration test uses mocked native IPC to verify wrong-PIN rejection,
unlock-before-stream ordering, microphone/return controls, and session cleanup.
It is not an end-to-end hardware meeting test.

Repository Python tests: `tests/test-desktop-engine.py` and
`tests/test-microphone-mute.py`, plus the existing audio and lifecycle regression suites.
The frozen-engine smoke test uses a temporary empty profile and opens no media:

```bash
python scripts/smoke-engine.py "src-tauri/target/release/bundle/macos/NetBridge Studio.app/Contents/Resources/engine"
```

Close the old NetBridge Source app before opening Studio: both need the same media
ports. The desktop engine refuses startup when the legacy port is occupied and holds
an OS lock to prevent competing desktop engines. It does not kill unrelated processes.
Logs: `~/.netbridge-source/logs/desktop-engine.log`.

Hardware release gate: a real presenter → mesh → Pi → USB meeting test, camera/mic
permission prompts on a clean Mac, return audio, device unplug/replug, mute privacy,
sleep/wake, app quit, and a signed update/rollback rehearsal. These cannot be replaced
by a UI screenshot or a successful compiler run.

## Connection health timing

Go live wakes the bridge sampler immediately. The bridge takes about two seconds
per measurement; subsequent samples target a five-second cadence, including that
sampling time. Studio reads the cached result every second. Slow network requests
can take longer, and measurements are never fabricated while waiting.

The 30-second startup grace applies only to corrective actions. Automatic repair
evaluation remains limited to once per ten seconds, with the existing consecutive
failure requirement, so faster display updates do not cause more media restarts.

## GitHub Actions releases

Run **Release NetBridge Studio (Windows + macOS)** (`release-studio.yml`) with a
new version. It prepares media runtimes, builds both platforms, tests them, and
creates one draft release with installers and checksums. No runtime ZIP is needed.
Optional inputs enable Apple signing/notarization and updater signatures when
credentials are configured. Windows installers remain unsigned pending customer
signing integration. Real Pi/meeting qualification is still required.
See [the release runbook](../../release.md#trigger-releases-from-github-actions-recommended).
Mobile is outside the current scope.

## Studio 0.1.11 UX (2026-09-30)

Email sign-in now uses six digits plus the receiving email address. Fleet and the
new presenter engine must ship together; older presenter binaries do not send
the email during redemption. Existing signed-in sessions and pending long
invitation codes remain valid. Upgrade presenter apps before deploying the matching
Fleet sign-in changes; older signed-out apps cannot redeem the new short codes.

Camera-off uses the existing authenticated video route to send plain black H.264
frames without camera capture, while microphone and return audio run independently.
This needs no Pi image change. The Pi's interruption freeze/black policy remains
in place if the connection itself fails. Validate mute/unmute on physical Mac,
Windows and the receiving meeting laptop before release.

Session history is local, migrated from the existing localStorage list to IndexedDB
and read in pages of eight. It still records sessions ended from this app, not
Fleet-wide meeting attendance or recordings.

Studio 0.1.12 bounds offline-bridge presence checks to one 1.5-second budget across
the entire list, with at most eight parallel probes. The last selected bridge is
checked first; probes only use known literal IP addresses. A responding bridge
can still override a stale Fleet heartbeat, while unsuccessful or unfinished checks
leave Fleet's status unchanged. Loading no longer adds several seconds per offline
bridge, and no background result can mutate a response already shown to the user.
