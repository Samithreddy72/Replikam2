# NetBridge release and Pi provisioning guide

Last reviewed: 18 September 2026.

This runbook covers **flashing a new Raspberry Pi** and **releasing NetBridge
Studio**, the Tauri desktop app replacing the browser-based presenter setup.
It does not cover releasing or installing the old presenter app.
Commands run from the repository root unless a
step explicitly changes directory. Replace uppercase example values before use.

## Release types and current support

| Deliverable | Purpose | Release process |
| --- | --- | --- |
| `netbridge-os-VERSION-SHA.img.xz` | Flash an entire Pi SD card | `build-image.yml` |
| `rootfs.tar.zst` | Update a standby OS slot on an existing Pi | Pi OTA; **never flash this with Imager** |
| `NetBridge Studio.app` / Studio ZIP or DMG | macOS presenter app with bundled media engine | Section 3 |
| Studio Windows `*-setup.exe` | Windows x64 presenter app with bundled media engine | Section 4; dedicated candidate CI |

Studio has been tested on **macOS Apple Silicon**. Windows and Intel Mac Studio
releases require their own matching native runtimes and hardware validation.
**CI migration note:** `build-app.yml` / `app-v*` still target the old app, not
Studio. Do not use them for a Studio release. Local macOS packaging is in section 3;
`release-studio.yml` builds Windows and macOS together; see the trigger steps below.

Studio's updater code exists, but its default endpoint and public key are empty.
Current developer builds are not notarized customer releases. Publishing a ZIP
alone does not enable in-app updates.

## Trigger releases from GitHub Actions (recommended)

After these workflows are committed and pushed to GitHub, open the repository's
**Actions** tab. A new manual workflow must be present on the default branch before
GitHub exposes its **Run workflow** button. Select the intended source branch/ref.

| Workflow in Actions | File | What to enter | Result |
| --- | --- | --- | --- |
| **Release NetBridge Pi image** | `build-image.yml` | Optional OS version; keep `draft` checked | Signed full SD image, OTA rootfs, manifests, signatures |
| **Release NetBridge Studio (Windows + macOS)** | `release-studio.yml` | Required Studio version, e.g. `0.1.1`; keep `draft` checked | Windows x64 installer plus macOS Apple Silicon ZIP/DMG, checksums and build records |

These are separate release operations. Updating Studio does not require rebuilding
a Pi image. Mobile is not part of either workflow.

### One-time repository configuration

In **Settings → Secrets and variables → Actions**, configure:

| Type | Name | Required for |
| --- | --- | --- |
| Variable | `FLEET_CONTROL_URL` | Pi; HTTPS fleet address |
| Secret | `FLEET_BOOTSTRAP_TOKEN` | Pi; fleet enrollment |
| Secret | `OTA_SIGNING_KEY` | Pi; PEM private key matching the established image signing trust |
| Secret | `APPLE_CERTIFICATE` | Optional Mac signing; base64 Developer ID Application `.p12` |
| Secret | `APPLE_CERTIFICATE_PASSWORD` | Optional Mac signing; `.p12` password |
| Secret | `APPLE_SIGNING_IDENTITY` | Optional Mac signing; full Developer ID Application identity |
| Secret | `APPLE_API_KEY_CONTENT` | Optional Mac notarization; `.p8` file contents |
| Secret | `APPLE_API_KEY` | Optional Mac notarization; API key ID |
| Secret | `APPLE_API_ISSUER` | Optional Mac notarization; issuer ID |
| Variable | `STUDIO_UPDATE_ENDPOINT` | Optional updater; stable HTTPS manifest URL |
| Variable | `STUDIO_UPDATE_PUBLIC_KEY` | Optional updater; public key contents |
| Secret | `TAURI_SIGNING_PRIVATE_KEY` | Optional updater; private key contents |
| Secret | `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Optional updater; key password if set |

Studio's default build needs **no prebuilt runtime ZIP or runtime URL secret**.
The runners assemble FFmpeg/GStreamer themselves, verify required media elements
and capture/encoder support, compile the mesh helper, and record runtime file hashes.
Windows GStreamer uses a pinned installer checksum. Mac GStreamer comes from
Homebrew; FFmpeg uses the existing platform download URLs, so these inputs are not
fully reproducible or independently checksum-pinned. Review their provenance and
retain the recorded runtime hashes when qualifying a release.

Select `macos_sign` only after all Apple secrets are configured. Select `updater`
only after configuring its endpoint and signing keys. Missing requested credentials
fail the build. Without these options, Studio produces development artifacts;
Windows Authenticode signing is **not configured by this workflow**.

### Run from the command line

```bash
# Pi: blank version uses root VERSION; output adds the source SHA.
gh workflow run build-image.yml --ref main --repo Samithreddy72/Replikam2 \
  -f draft=true

# Studio: version is applied consistently to npm, Tauri, Cargo, and the UI in CI.
gh workflow run release-studio.yml --ref main --repo Samithreddy72/Replikam2 \
  -f version=0.1.1 -f draft=true -f macos_sign=false -f updater=false

gh run list --repo Samithreddy72/Replikam2 --limit 10
gh run watch REPLACE_WITH_RUN_ID --repo Samithreddy72/Replikam2 --exit-status
```

Choose a new version instead of reusing a published release. Studio uses
`studio-vVERSION`; Pi uses `vVERSION-SHA`. Tags point to the source commit built,
and build records retain the selected version. Studio's input version is applied
in the runner checkout; CI does not commit version edits back to the branch.

Studio's release is created only when **both** platform builds and automated checks
pass. A successful platform's Actions artifact remains downloadable if the other
fails. Draft retries may replace assets only when the source commit matches;
published versions cannot be overwritten by the release scripts.

Leave `draft=true` for review and physical testing. Then publish the draft from
GitHub Releases. If deliberately choosing `draft=false`, Pi is published after
upload and Studio is published as a **prerelease**, since Windows customer signing
and physical qualification are not automated. Compilation does not prove Pi boot
or real meeting audio/video: use the tests in this guide before promoting a release.

The workflows retain downloadable Actions artifacts (Pi: seven days; Studio:
14 days), separately from release assets. Studio's updater artifacts are attached
when requested, but the action does not deploy an update manifest to your hosting
service. Complete section 3.8 before expecting in-app updates.

## 1. Flash a new Pi

### 1.1 Prepare hardware and access

- Raspberry Pi **4 Model B**, 2 GB RAM or more.
- microSD card, **16 GB minimum**, card reader, and a spare known-good card.
- A reliable Pi power arrangement and a short **USB-C data cable** for the meeting laptop.
- A phone for Wi-Fi setup, the device's setup-network password/label, and venue Wi-Fi credentials.
- Fleet admin access to `https://fleet.scine.online` and access to the private repository's releases.

Keep the only working SD card as a rollback. Flashing erases the selected card.
Keep the meeting laptop disconnected during initial provisioning.

### 1.2 Select the image

Open [repository releases](https://github.com/Samithreddy72/Replikam2/releases).
Choose an image whose **exact commit and spare-card boot test** have been approved.
Do not infer approval from “Latest” or the highest version number: historical image
labels were inconsistent. Current builds default to drafts; publishing alone does
not certify a physical boot test.

Download these files from the **same release**:

- `netbridge-os-VERSION-SHA.img.xz`
- `manifest-disk.txt`
- `manifest-disk.txt.sig`
- `ota-pubkey.pem`

Use the complete release tag, including its SHA suffix. Example download commands:

```bash
export NB_IMAGE_TAG='REPLACE_WITH_APPROVED_IMAGE_RELEASE_TAG'
mkdir -p "$HOME/Downloads/netbridge-image/$NB_IMAGE_TAG"
gh release download "$NB_IMAGE_TAG" \
  --repo Samithreddy72/Replikam2 \
  --dir "$HOME/Downloads/netbridge-image/$NB_IMAGE_TAG" \
  --pattern '*.img.xz' --pattern 'manifest-disk.txt*' --pattern 'ota-pubkey.pem'
```

Factory images contain a fleet enrollment token. Keep them in approved private
storage; do not mirror them to a public download site.

### 1.3 Verify before flashing

Run inside the downloaded release directory:

```bash
cd "$HOME/Downloads/netbridge-image/$NB_IMAGE_TAG"
openssl dgst -sha256 -verify ota-pubkey.pem \
  -signature manifest-disk.txt.sig manifest-disk.txt
python3 - <<'PY'
import hashlib
from pathlib import Path
manifest = dict(line.split('=', 1) for line in Path('manifest-disk.txt').read_text().splitlines() if '=' in line)
image = Path(manifest['image'])
assert image.name == str(image) and image.name.endswith('.img.xz'), 'Unexpected image filename'
assert image.stat().st_size == int(manifest['size']), 'Image size mismatch'
hash_value = hashlib.sha256()
with image.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        hash_value.update(chunk)
assert hash_value.hexdigest() == manifest['sha256'], 'Image checksum mismatch'
print('PASS:', image.name, 'version:', manifest['version'])
PY
```

Expect `Verified OK` and `PASS`. Obtain/compare the OTA public key against your
release owner's trusted copy: a key downloaded alongside an image is not, by
itself, independent proof of the publisher. Stop on any verification failure.

For release qualification on a Mac, also decompress a copy and run the repository
image audit. This needs `xz` and `debugfs` from Homebrew `e2fsprogs`, plus enough
space for the uncompressed disk:

```bash
# Back at the repository root; replace both paths.
xz -dc '/path/to/netbridge-os-VERSION-SHA.img.xz' > '/path/to/netbridge-candidate.img'
bash tools/image-audit.sh '/path/to/netbridge-candidate.img'
```

Resolve failures and unreadable checks before approving an image. The audit
checks image contents; it cannot prove that the card boots or media works.

### 1.4 Flash with Raspberry Pi Imager

1. Open Raspberry Pi Imager from the [official download page](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi 4**.
3. Choose **Use custom** and select the verified `.img.xz` file.
4. Select the SD card. Check its capacity and identity before writing.
5. Skip OS customization: do not inject a hostname, SSH user, password, or Wi-Fi settings.
6. Write the image and let Imager finish verification.
7. Eject the card and insert it into the powered-off Pi.

### 1.5 Boot, connect Wi-Fi, and claim

1. Power the Pi and allow around 90 seconds for first boot; slower cards may take longer.
2. On a phone, join `BridgeSetup-XXXX` using the device's setup password.
3. Use the captive portal to select venue Wi-Fi and enter its password.
4. Wait for Wi-Fi, internet, and fleet registration to succeed. The setup hotspot then disappears.
5. Sign in to the fleet as an admin. Find the **Unclaimed** bridge and match its pairing code/device identity.
6. **Claim** it and give it a useful name, such as `London Meeting Room`.
7. Use **Set / rotate PIN** to set a six-digit presenter PIN. Share it separately with the authorized presenter.

The setup Wi-Fi password and presenter PIN are different credentials. Current
source generates a random setup password per device; do not assume an old shared
default. Factory provisioning must retrieve it through an authorized device
session (`bridge setup-pass`) and supply the label. If the password is missing,
resolve that with the provisioner before shipping the device. Reflashing can
invalidate a previous card's setup label.

Optional diagnostic SSH is a separately provisioned facility; it is not guaranteed
to be enabled on a factory image. See [diagnostic SSH setup](pi/diagnostics/README.md).

### 1.6 Accept the device only after a real meeting test

1. Connect the Pi's USB-C data port to the meeting laptop.
2. Select the NetBridge USB **camera, microphone, and speaker** in Jitsi/Zoom/Teams/Meet.
3. On the presenter Mac, open Studio, sign in, select the claimed bridge, enter its PIN, and go live.
4. Have another meeting participant confirm the presenter's **moving picture and voice**.
5. Confirm the presenter hears the other participant through return audio.
6. Open Studio's Connection health: verify video delivery, voice delivery, return audio, and USB camera.
7. Mute the presenter microphone and verify speech stops; unmute and verify it returns.
8. End the session. On an image containing the current idle renderer, the USB camera shows **plain black**.
9. Power-cycle outside the meeting and verify Wi-Fi reconnection, fleet registration, and another session.

Record image tag, manifest version, commit, device ID, test date, and tester. An
older approved image may still show the former status card; changing source does
not update a downloaded image or a flashed card.

### 1.7 Troubleshooting and rollback

| Symptom | Check first |
| --- | --- |
| Setup hotspot absent | Allow first boot to finish; check whether saved Wi-Fi already connected; verify power and image/card integrity. |
| Hotspot password rejected | Use the password for this card; do not derive it from the SSID. |
| Pi absent from fleet | Venue internet access, enrollment configuration, and the build's fleet URL/token. |
| USB camera absent | USB-C **data** cable and meeting-app device selection. |
| Video/voice missing but return audio works | Studio Connection health and presenter logs; confirm receiver-side delivery rather than merely a running sender. |
| Reboots/stuttering | Fleet power diagnostics; investigate supply/cable problems before changing media buffers. |

For rollback, end the meeting, disconnect the meeting laptop, power off, and
restore the retained known-good card. Do not restart `bridge-uvcd` or the USB
gadget while the meeting laptop is connected, and do not unload `v4l2loopback`
on a live Pi. Retain the pinned `6.12.93+rpt-rpi-v8` kernel unless a replacement
has passed the project's hardware qualification. See [Golden Rules](docs/GOLDEN-RULES.md).

## 2. Build a new Pi image when needed

Skip this section when provisioning with an already-approved image.

1. Review the change and update root `VERSION` for the OS release; this is not the Studio version.
2. Commit and push the reviewed source to the intended build branch.
3. Ensure GitHub has variable `FLEET_CONTROL_URL` and secrets `FLEET_BOOTSTRAP_TOKEN`
   and `OTA_SIGNING_KEY`. All three are checked before the build starts.
   The enrollment token must be accepted by the target fleet.
   Keep the existing signing trust chain; do not casually replace signing keys.
4. From the release checkout, run `bash tools/pre-build-gate.sh`. It expects a clean
   tree matching `origin/main`; inspect any failure instead of bypassing it.
5. Dispatch the image workflow and monitor the matching run:

```bash
gh workflow run build-image.yml --ref main --repo Samithreddy72/Replikam2
gh run list --workflow build-image.yml --repo Samithreddy72/Replikam2 --limit 5
gh run watch REPLACE_WITH_RUN_ID --repo Samithreddy72/Replikam2 --exit-status
```

The workflow reads `VERSION` by default and appends the short commit SHA. A `v*`
tag also triggers it. It builds the full disk and OTA rootfs, signs manifests,
and creates draft `vVERSION-SHA`. Missing signing/enrollment credentials fail before
the expensive build. Tag-triggered runs also remain drafts.

Verify and boot-test a spare card before publishing the draft. If release upload
fails, inspect the run's `*-flashable` artifact (retained for seven days) and verify
it normally; never use partially downloaded assets.

`tools/stage-image.sh` currently assumes `v2.0.0-<commit>` tags. For other versions,
use the explicit download/verification steps above rather than that shortcut.

## 3. Release a new NetBridge Studio version (macOS and shared release steps)

### 3.1 Prepare the build machine

Use a macOS Apple Silicon build machine for the currently validated target.
Required tools: Node **22.14+**, Rust **1.88** (repository toolchain pin), Xcode
command-line tools, Python (the tested environment uses **3.11**), and Go matching
`app/netbridge-source/mesh/go.mod` (currently **1.26.5**).

```bash
python3.11 -m venv app/netbridge-source/.venv
app/netbridge-source/.venv/bin/python -m pip install pyinstaller certifi
npm ci --prefix app/netbridge-desktop
(cd app/netbridge-desktop && npx playwright install chromium)
```

Reuse an existing tested Python environment instead of recreating it during every
release. Record dependency versions with the release evidence.

For manual builds, the packager requires a **complete native media runtime** at
`app/netbridge-source/_bundle/runtime`, or at `NB_MEDIA_DIR`. It is not installed
by `npm ci`. Obtain the approved runtime from the release maintainer, including
FFmpeg, `gst/gst-launch-1.0`, GStreamer plugins, and their relocated native libraries.
Preserve the directory layout. The helper is freshly compiled from Go source during packaging.

The release action prepares this automatically. To prepare a fresh Mac runtime
locally, install Homebrew GStreamer and run the same helper against an empty directory:

```bash
brew install gstreamer
app/netbridge-source/.venv/bin/python app/netbridge-desktop/scripts/prepare-runtime.py \
  /ABSOLUTE/PATH/TO/EMPTY/studio-runtime
export NB_MEDIA_DIR='/ABSOLUTE/PATH/TO/EMPTY/studio-runtime'
```

The helper requires every configured GStreamer element to resolve from the bundled
runtime and checks FFmpeg capture/encoder capabilities. It does not launch the old
app or access cameras. Hardware and clean-machine testing remain required.

Optional build variables:

| Variable | Meaning |
| --- | --- |
| `NB_PYTHON` | Absolute path to the build Python with PyInstaller and certifi |
| `NB_MEDIA_DIR` | Absolute path to the approved media runtime directory |
| `NB_GO` | Go executable if it is not on `PATH` |
| `CARGO_TARGET_DIR` | Separate native build/output directory; useful while an older build is running |
| `NB_PERSISTENT_AUDIO=1` | Optional GI audio packaging; use only with a separately tested GI Python/runtime |

### 3.2 Set and record the version

In Actions, enter the new version: the workflow stamps these files automatically.
For a manual build, choose a new Studio SemVer, for example `0.1.1`, and update:

- `app/netbridge-desktop/package.json` and its `package-lock.json`.
- `app/netbridge-desktop/src-tauri/tauri.conf.json` → `version` (updater version).
- `app/netbridge-desktop/src-tauri/Cargo.toml` → package version; refresh `Cargo.lock` through Cargo.
The Studio UI and support report now read the npm package version automatically.

`npm version NEW_VERSION --no-git-tag-version --prefix app/netbridge-desktop`
updates the npm files only. It does not update the other files above.
Keep the bundle identifier `online.scine.netbridge.studio` stable.
The embedded engine's `APP_VERSION` and root OS `VERSION` are separate versions.

Commit the release changes, confirm the checkout is clean, and record its full SHA:

```bash
git status --short
git rev-parse HEAD
```

Use a distinct **`studio-vNEW_VERSION`** tag for Studio. This is a manual release
convention; it does not automatically trigger builds. Studio release CI is
manual-dispatch only. Reserve `v*` for Pi images.

### 3.3 Run checks before packaging

```bash
npm run build --prefix app/netbridge-desktop
npm test --prefix app/netbridge-desktop
npm run test:ui --prefix app/netbridge-desktop
(cd app/netbridge-desktop/src-tauri && cargo test)

for test in test-desktop-engine test-microphone-selection test-microphone-mute \
  test-bridge-health-refresh test-return-playback test-audio-api \
  test-session-stop test-stream-liveness test-mesh-helper-health test-tuning-relay; do
  app/netbridge-source/.venv/bin/python "tests/$test.py" || exit 1
done
```

UI tests use mocked native IPC. Passing them is not evidence of real media delivery.

### 3.4 Configure customer signing and optional in-app updates

For internal development, packaging can run without release credentials. Label
that result as a developer build. For customer distribution, install the Developer
ID Application certificate and configure notarization before packaging:

```bash
security find-identity -v -p codesigning
export APPLE_SIGNING_IDENTITY='Developer ID Application: YOUR_ORGANIZATION (TEAMID)'
# Load APPLE_API_ISSUER, APPLE_API_KEY, and APPLE_API_KEY_PATH from approved secret storage.
```

Alternatively, Tauri supports Apple ID notarization credentials. Follow the
[official macOS signing guide](https://v2.tauri.app/distribute/sign/macos/).
Apple signing and Studio updater signing are separate requirements. Verify nested
Python/media executables as well as the outer app; the current packager is not a
previously qualified customer-signing pipeline. A notarization failure blocks a
customer release. Do not make quarantine-removal commands part of customer installation.

For in-app updates, first provision a stable HTTPS manifest endpoint and retain a
Tauri updater keypair outside the repository. Generate the pair **once**, not per release:

```bash
# Replace with a protected location; never overwrite an existing release key.
(cd app/netbridge-desktop && npx tauri signer generate -w /PROTECTED/PATH/studio-update.key)
```

Configure these through the release environment:

| Variable | Value |
| --- | --- |
| `NB_UPDATE_ENDPOINT` | Actual HTTPS manifest URL; no endpoint is currently provisioned by this repository |
| `NB_UPDATE_PUBLIC_KEY` | Contents of the updater `.pub` file, not its filename |
| `TAURI_SIGNING_PRIVATE_KEY` | Updater private-key path or contents |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Key password, if configured |

The packaging script enables updater artifacts only when updater configuration is
provided. Preserve the keypair: installed apps trust that public key. Existing
builds with an empty endpoint/key need a manual whole-app installation to join the
update channel. See [Tauri updater documentation](https://v2.tauri.app/plugin/updater/).

### 3.5 Package and inspect

```bash
npm run package --prefix app/netbridge-desktop -- --bundles app,dmg
```

The command freezes Python, copies the native runtime, compiles the mesh helper
from source, builds React/Rust, and bundles the app. Do not replace this with
`npm run build`, which only produces the web frontend.

Default outputs are below `app/netbridge-desktop/src-tauri/target/release/bundle/`:

- `macos/NetBridge Studio.app`
- `dmg/*.dmg`
- With updater signing enabled, the generated macOS `.app.tar.gz` and its `.sig`.

If using `CARGO_TARGET_DIR`, read outputs under that directory instead. Build in a
separate target directory when the default output app is in use; do not overwrite
or restart a running meeting session.

```bash
export NB_APP='app/netbridge-desktop/src-tauri/target/release/bundle/macos/NetBridge Studio.app'
python3 app/netbridge-desktop/scripts/smoke-engine.py "$NB_APP/Contents/Resources/engine"
python3 tools/verify-gst-bundle.py "$NB_APP/Contents/Resources/engine/runtime/gst"

# Required for the signed customer build:
codesign --verify --deep --strict --verbose=2 "$NB_APP"
spctl --assess --type execute --verbose=2 "$NB_APP"
xcrun stapler validate "$NB_APP"
```

The engine smoke test uses an empty temporary profile and opens no media. If the
GStreamer verifier falls back to filename-only checks because `gst-inspect-1.0`
is missing, record that limitation; it is not a successful runtime pipeline test.
Verify the notarization result and test the actual downloadable package on a clean
Mac. If nested signing needs corrections, regenerate and revalidate the final
installer and updater archive before publication.

### 3.6 Test the final build on hardware

Use the same Pi/meeting acceptance test as section 1.6, plus:

- Camera and microphone permission prompts on a clean Mac.
- First health result after the bridge's roughly two-second sample; subsequent
  checks target five seconds, with the display reading cached state each second.
- Microphone default-device change/reconnect, return-audio toggle/recovery, and sleep/wake.
- End session and quit: capture stops, camera light goes off, and child media/mesh processes exit.
- For an update-enabled build: install from the previous signed version, retain sign-in,
  verify both media directions, and confirm installation is refused during a live session.

Record failures and successful evidence against the **final artifact's checksum**.
Do not test one build and then publish a rebuilt or re-signed artifact without rechecking it.

### 3.7 Publish the release

Create a versioned ZIP of the final app if distributing ZIPs. Keep installer files
and updater archives distinct: the custom ZIP below is not the updater artifact.

```bash
export NB_STUDIO_VERSION='REPLACE_WITH_NEW_VERSION'
export NB_RELEASE_COMMIT='REPLACE_WITH_FULL_TESTED_COMMIT_SHA'
export NB_RELEASE_DIR="$PWD/app/netbridge-desktop/artifacts/studio-$NB_STUDIO_VERSION"
mkdir -p "$NB_RELEASE_DIR"
ditto -c -k --sequesterRsrc --keepParent "$NB_APP" \
  "$NB_RELEASE_DIR/NetBridge-Studio-$NB_STUDIO_VERSION-macos-arm64.zip"
(cd "$NB_RELEASE_DIR" && shasum -a 256 *.zip > SHA256SUMS.txt)
```

Add the final DMG and generated updater archive/signature as applicable; include
all downloadable binaries in your checksum manifest. Write release notes containing
version, full source SHA, platform, changes, known limitations, tested Pi image,
verification results, upgrade steps, and rollback instructions.

```bash
# Create release-notes.md in NB_RELEASE_DIR before running this.
gh release create "studio-v$NB_STUDIO_VERSION" \
  --repo Samithreddy72/Replikam2 --target "$NB_RELEASE_COMMIT" --draft \
  --title "NetBridge Studio $NB_STUDIO_VERSION" \
  --notes-file "$NB_RELEASE_DIR/release-notes.md"
gh release upload "studio-v$NB_STUDIO_VERSION" \
  --repo Samithreddy72/Replikam2 \
  "$NB_RELEASE_DIR/NetBridge-Studio-$NB_STUDIO_VERSION-macos-arm64.zip" \
  "$NB_RELEASE_DIR/SHA256SUMS.txt"
# Upload the other final artifacts too, inspect the draft, then publish:
gh release edit "studio-v$NB_STUDIO_VERSION" \
  --repo Samithreddy72/Replikam2 --draft=false --latest=false
```

Use new immutable versioned assets; do not replace bytes under a released version.
The repository is private: its release URLs are not automatically accessible to
customer apps. The current updater has no GitHub authentication headers, so host
update artifacts on an approved HTTPS service reachable by those apps. Never embed
a GitHub/admin token in the app to make a private download work.

### 3.8 Publish the update manifest last

For a configured update channel, upload the generated archive first, verify its
download, then publish the manifest. Example for Apple Silicon:

```json
{
  "version": "0.1.1",
  "notes": "Describe this release.",
  "pub_date": "2026-09-18T15:00:00Z",
  "platforms": {
    "darwin-aarch64": {
      "url": "https://YOUR_DOWNLOAD_HOST/studio/0.1.1/NetBridge-Studio.app.tar.gz",
      "signature": "REPLACE_WITH_CONTENTS_OF_THE_MATCHING_SIG_FILE"
    }
  }
}
```

Replace every example value. The signature is the `.sig` **contents**, not a URL
or SHA256. Use the archive generated for that exact signed build. The manifest's
platform key and structure follow [Tauri's static updater format](https://v2.tauri.app/plugin/updater/).

Customers use **Settings → Check for updates → Install and restart**, after ending
their session. Studio does not currently force remote installs, schedule background
updates, or implement staged rollout policy. The release host must supply those
policies if they are needed.

### 3.9 Roll back Studio

If a release fails, stop advertising it on the update endpoint and pause distribution.
Retain the previous signed whole-app installer and release evidence.

For an affected user, end the session, quit Studio, and install the retained
compatible build. Preserve `~/.netbridge-source` rather than deleting the account
profile. Do not replace only the Python executable or mesh binary inside a signed
app: distribute the whole validated bundle.

The normal updater compares versions; pointing its manifest to a lower version
is not an automatic downgrade. For remote recovery, ship the known-good code as a
**new higher patch version**, rebuild/sign/test it, and publish that version.

## 4. Release NetBridge Studio for Windows

**Target: Windows x64, initially qualified on Windows 11.** Use the combined
**Release NetBridge Studio (Windows + macOS)** action described above. It builds
the new Studio application, not the browser-based presenter app. The workflow
has not yet been validated by an actual hosted Windows run and physical meeting test.
Mobile is out of scope.

### 4.1 Windows build inputs

The action installs Python 3.11, Node 22.14, Rust 1.88, Go from `mesh/go.mod`, and
the official GStreamer MSVC x64 runtime (version 1.26.11, checksum pinned in YAML).
It assembles the Windows runtime using `scripts/prepare-runtime.py`; customers do
not install Python, Go, FFmpeg, or GStreamer separately. Retain the generated
runtime build record with its component versions and file hashes.

### 4.2 Download and qualify the installer

The action tests Studio, builds the NSIS installer, smoke-tests the embedded
engine, and attaches the following to `studio-vVERSION` alongside the Mac assets:

- `NetBridge-Studio-VERSION-windows-x64-setup.exe`
- `windows-x64-SHA256SUMS.txt`
- `windows-x64-build-info.json` and `windows-x64-runtime-build.json`
- Windows updater payload/signature when updater signing was requested.

Run artifacts are retained for 14 days. Default releases are drafts. The workflow
builds an **unsigned Windows installer**, so complete signing and the following
hardware qualification before customer distribution.

### 4.3 Build locally on Windows instead

Install Node 22.14+, Python 3.11, the pinned Rust/MSVC toolchain, Go matching
`mesh/go.mod`, Microsoft C++ Build Tools with the Windows SDK, and WebView2.
See [Tauri Windows prerequisites](https://v2.tauri.app/start/prerequisites/).
Run from the repository root in PowerShell, with the approved runtime extracted:

```powershell
py -3.11 -m venv app/netbridge-source/.venv
& app/netbridge-source/.venv/Scripts/python.exe -m pip install pyinstaller==6.22.3 certifi
npm ci --prefix app/netbridge-desktop
$env:NB_PYTHON = (Resolve-Path app/netbridge-source/.venv/Scripts/python.exe).Path
$env:NB_MEDIA_DIR = 'C:\REPLACE_WITH_APPROVED_RUNTIME'
npm run package --prefix app/netbridge-desktop -- --bundles nsis
if ($LASTEXITCODE) { throw 'Studio packaging failed' }
& $env:NB_PYTHON app/netbridge-desktop/scripts/smoke-engine.py app/netbridge-desktop/src-tauri/resources/NetBridgeEngine
```

Default installer output: `app/netbridge-desktop/src-tauri/target/release/bundle/nsis/`.
Use a Windows machine/runner for this complete build: the embedded Python and
media components must match the target OS. The package script launches Tauri's
JavaScript entry point directly to avoid spawning a Windows `.cmd` shim as an executable.

### 4.4 Qualify the installer on a clean Windows PC

Install the candidate on a physical Windows test PC without Python, Go, FFmpeg,
or GStreamer installed. Check WebView2 installation behavior; an offline deployment
needs a separately configured and tested WebView2 provisioning option. Tauri's
[Windows installer guide](https://v2.tauri.app/distribute/windows-installer/) covers
NSIS, MSI, and WebView2 packaging.

Repeat section 3.6's hardware meeting tests, specifically:

- Windows camera/microphone privacy permissions and correct device enumeration.
- Built-in and USB camera/microphone selection using DirectShow.
- Both outgoing media legs reaching the Pi, and return playback through Windows audio.
- Headphones, device disconnect/reconnect, sleep/wake, mute, end session, and full app quit.
- Standard-user installation, launch, upgrade, and uninstall; no console window required.
- Installation from the actual downloaded artifact, not only the CI working directory.

A hosted runner's smoke test does not validate cameras, speakers, the Pi, or Jitsi.
Record actual tested Windows versions; test other Windows versions separately
before advertising them as supported.

### 4.5 Sign, publish, and update Windows releases

Customer Windows signing is still a release prerequisite. Provision the
organization's Windows code-signing service/certificate and integrate signing
into the Tauri build before enabling customer distribution. The release workflow
does not currently configure this. An unsigned candidate is for internal QA.

Verify Authenticode on the installed Studio executable, applicable bundled
executables, and the final installer. Use `Get-AuthenticodeSignature` or the
Windows SDK's `signtool verify /pa /v`. Recompute checksums and repeat acceptance
checks after producing the final signed artifact.

Attach the tested Windows installer, checksum, and build identity to the same
`studio-vVERSION` release as macOS, following section 3.7. Release notes must say
which platforms are actually available; a failed Windows build must not be hidden
by a successful Mac build.

For in-app updates, configure the same `NB_UPDATE_*` / `TAURI_SIGNING_*` settings
from section 3.4 in the Windows release environment. Add a **`windows-x86_64`**
entry to the manifest, using the Windows updater artifact generated by that build
and its matching `.sig` contents. Keep the Mac entry intact; do not point Windows
at the Mac archive. Rehearse an update from the prior signed Windows installer
before advertising it. Tauri updater signatures do not replace Authenticode.

Recover a bad Windows release using the same whole-app rollback or higher-version
recovery release described in section 3.9. Preserve the user's
`%USERPROFILE%\.netbridge-source` profile and never replace individual files inside
an installed bundle during an active meeting.

## 5. Release record template

Keep this with every approved release, including private hardware test evidence:

```text
Product: NetBridge OS / NetBridge Studio
Version and release tag:
Full source commit:
Build workflow/run or build-machine tool versions:
Artifact names and SHA256 checksums:
Signing/notarization verification:
Tested Studio version + Pi image version:
Test device and hardware-test results:
Known limitations:
Release owner and date:
Previous known-good release / rollback location:
```

Related references: [Studio implementation](app/netbridge-desktop/README.md),
[additional Pi provisioning details](docs/SETUP-GUIDE.md), [handover and key ownership](docs/HANDOVER.md),
[image workflow](.github/workflows/build-image.yml), and
[Studio packaging script](app/netbridge-desktop/scripts/package.mjs).
