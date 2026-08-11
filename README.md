# NetBridge

**A Raspberry Pi that pretends to be a webcam, so a presenter anywhere can appear in a
meeting anywhere.**

Plug the Pi into a meeting laptop with a USB-C cable. It shows up in Zoom, Teams or Meet as
an ordinary camera, microphone and speaker — **nothing is installed on that laptop**. A
presenter somewhere else runs the NetBridge app; their face and voice come out of that fake
webcam, and the room's audio comes back to them. The two ends find each other over a private
encrypted mesh, so neither needs a public IP, a port forward, or the same network.

> **New here?** Go straight to the [Setup Guide](docs/SETUP-GUIDE.md) — blank SD card to a
> working bridge, written for someone who has never touched a Raspberry Pi.

---

## Why it exists

Remote presenters normally join a meeting *as a participant on a screen*. NetBridge makes
them appear *as the room's camera* — full-frame, in the same video tile a physical camera
would occupy, on a laptop nobody had to configure. That matters for interviews, briefings,
teaching, and anywhere a guest must look like they are in the room.

The whole design protects one promise: **the meeting laptop installs nothing.** Everything
else — the mesh, the fleet, the self-provisioning image — exists to keep that true.

---

## Features

| | |
|---|---|
| **Zero-install at the meeting end** | Standard USB Video/Audio Class. Any OS, any conferencing app. |
| **Zero per-device configuration** | Every SD card is identical. A bridge sets itself up from a phone. |
| **Works across continents** | Encrypted mesh with NAT traversal. No port forwarding, no static IPs. |
| **Fleet-managed** | Every bridge on one page, with the known fixes one click away. |
| **Remote repair** | Push a signed code fix to a bridge in another country, or roll one back. |
| **Self-healing** | Watchdogs, auto-rollback on a bad deploy, read-only root, survives power cuts. |
| **Follows the meeting's audio rate** | 32 / 44.1 / 48 kHz, switched live, no restart. |
| **PIN-gated** | The device itself checks the PIN before accepting a single frame. |

---

## Architecture

```
    PRESENTER                     PRIVATE MESH                  MEETING ROOM
 ┌──────────────┐                                          ┌──────────────────┐
 │ NetBridge    │  video 5000  ─────────────────────────►  │  Raspberry Pi 4  │  USB-C
 │ app (Mac)    │  voice 5002  ─────────────────────────►  │                  │ ═══════►
 │              │                                          │  UVC camera      │  laptop
 │  camera ─────┤  ◄───────────────────────  audio 5004    │  UAC2 mic/spkr   │
 │  mic ────────┤                                          └────────┬─────────┘
 └──────┬───────┘                                                   │
        │                                                           │ telemetry
        │            ┌───────────────────────────────┐              │ + commands
        └───────────►│   Fleet control plane (AWS)   │◄─────────────┘
           sign-in   │   fleet.scine.online          │
           mesh key  │   panel · API · alerts        │
                     └───────────────────────────────┘
```

**Every media leg rides the mesh.** There is deliberately no LAN path: if the mesh cannot be
established the app refuses with a reason rather than silently downgrading.

The control plane never carries media — only telemetry, commands and mesh keys. Deploying it
does not interrupt a live stream.

---

## Requirements

**Hardware:** Raspberry Pi 4 Model B (2 GB+), microSD 16 GB+, a power supply rated for the
Pi 4's peak draw, and a **USB-C data cable** (charge-only cables are the most common setup
failure).

**Presenter:** macOS (Apple Silicon) or Windows 10/11. **Nothing to install** — the app
bundles its own ffmpeg and GStreamer. Download it from
[Releases](https://github.com/Samithreddy72/Replikam2/releases?q=app-v).

**Network:** ordinary Wi-Fi at both ends. No router changes.

---

## Quick start

**The bridge** (once per device):

```bash
# 1. Download the newest netbridge-os-*.img.xz from Releases
# 2. Flash it with Raspberry Pi Imager — pick "Use custom image", change no settings
# 3. Boot the Pi. From a phone, join the "BridgeSetup-XXXX" Wi-Fi network it raises
#    and hand it the venue's Wi-Fi through the page that opens.
# 4. It appears at https://fleet.scine.online as "Unclaimed · just joined" — click Claim,
#    give it a name, and set a 6-digit PIN.
```

**The meeting room** (each meeting):

```bash
# 5. Plug the Pi into the meeting laptop with a USB-C DATA cable (not charge-only).
#    In Zoom/Teams/Meet pick "NetBridge" as camera, microphone AND speaker.
#    Nothing is installed on that laptop.
```

**The presenter** (each meeting):

```bash
# 6. Download the app for your platform from Releases (the app-v* release),
#    unzip it, and keep every file in the folder together — the mesh helper
#    beside the app is what carries the meeting's audio back to you.
#       macOS   : xattr -dr com.apple.quarantine NetBridgeSource   (once, after download)
#       Windows : SmartScreen -> "More info" -> "Run anyway"       (once)
# 7. Run it. First launch takes ~15s while it unpacks; a browser tab opens at
#    http://127.0.0.1:8765/
# 8. Sign in with your work email, pick the bridge, enter the PIN, Unlock, Go live.
```

**Before an important session**, run the preflight — it checks the four things that
actually go wrong (camera not delivering, bridge rebooted, board browning out, media not
arriving at the far end) and tells you what to do about each:

```bash
bash tools/preflight.sh
```

Full detail, including what you should see at each step:
**[docs/SETUP-GUIDE.md](docs/SETUP-GUIDE.md)**

---

## Documentation

| Document | What it covers |
|---|---|
| [SETUP-GUIDE.md](docs/SETUP-GUIDE.md) | **Start here.** Blank SD card → working bridge, in ten phases |
| [FLEET.md](docs/FLEET.md) | The fleet page: every action, what it fixes, what interrupts a stream |
| [REMOTE-RECOVERY.md](docs/REMOTE-RECOVERY.md) | Fixing a bridge you cannot physically reach |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Deeper diagnostics |
| [AUDIO-TUNING.md](docs/AUDIO-TUNING.md) | The return-audio pipeline and its tunables |
| [PROVISIONING-V2.md](docs/PROVISIONING-V2.md) | How a card enrols itself |
| [GOLDEN-RULES.md](docs/GOLDEN-RULES.md) | Hard-won constraints — read before changing the media path |
| [CHANGELOG.md](CHANGELOG.md) | Verified restore points and released builds |
| [tools/preflight.sh](tools/preflight.sh) | Run before going live: camera, bridge, power, gates, path jitter |
| [tools/fix-camera-macos.sh](tools/fix-camera-macos.sh) | Clears a wedged macOS camera (opens but sends no frames) |
| [control-plane/README.md](control-plane/README.md) | Deploying and extending the fleet server |

---

## Repository layout

```
pi/            what runs on the bridge — scripts/ and systemd/ units
app/           the presenter app (netbridge-source) + its embedded mesh client
control-plane/ the fleet server: FastAPI backend, web panel, AWS deploy kit
factory/       CI image build — turns this repo into a flashable OS image
tools/         operator tooling: preflight, sign-and-publish, card surgery
tests/         test suites that run without a Pi
docs/          the documentation above
restore/       verified restore points
```

---

## Updating things

Three places code lives, three different routes:

| Changing | How | Interrupts? |
|---|---|---|
| Fleet panel / API | `bash control-plane/deploy/aws/deploy.sh <HOST>` | No |
| Presenter app | Relaunch it | Ends the session |
| A media script on the bridge | `bash tools/publish-script.sh <file>` → **Actions → Push a code fix** | Restarts one service |
| Agent, bridge-web, units, boot config | `gh workflow run build-image.yml` → flash the card | Yes |

The third row is the important one: it works for a bridge **anywhere in the world**, because
the bridge fetches the signed payload from the fleet over the same HTTPS it already uses.

---

## Security

- **The meeting laptop installs nothing and is never trusted with credentials.**
- **No pre-auth keys on SD cards.** A bridge's mesh key is issued when you claim it.
- **PINs are verified on the device**, not just in the cloud, and never displayed after being set.
- **Remote code is signed.** A script override is verified against a public key on the
  read-only root — at install, and again at every service start. Auto-rollback quarantines
  anything that crash-loops.
- **Per-admin accounts, magic-link sign-in, org scoping, and an audit log.**
- Secrets live in `/opt/netbridge/.env` on the fleet host and `~/.netbridge/` on the
  operator's machine. Never in the repository.

> ⚠️ The enrolment token is baked into the OS image, so anyone holding an image file can add
> a device to your fleet. That is inherent to "no per-device configuration" — treat image
> files as private and rotate `BOOTSTRAP_TOKENS` if one leaks.

---

## Development

```bash
git clone https://github.com/Samithreddy72/Replikam2.git
cd Replikam2
bash tests/test-script-override.sh      # signed-override loader, 23 checks
bash tests/test-return-rate-follow.sh   # live sample-rate following, 20 checks
```

Both run on a laptop with no Pi attached.

Before changing anything in the media path, read [GOLDEN-RULES.md](docs/GOLDEN-RULES.md) —
several of those constraints were learned by breaking a live call.

---

## Status and honesty

This is a working system, not a finished product. Kept deliberately visible:

- **Echo cancellation is built but not enabled.** A presenter who is also audible in the
  meeting may hear themselves. Use headphones.
- **The presenter app is ad-hoc signed, not notarised.** The packaged macOS binary runs
  (an earlier `CODESIGNING / Invalid Page` kill cleared after a reboot), but macOS may still
  quarantine a *freshly downloaded* copy — clear it with
  `xattr -dr com.apple.quarantine NetBridgeSource`. Proper notarisation needs an Apple
  Developer account. First launch takes ~15s while the one-file bundle unpacks.
- **Under-voltage on the reference hardware is unresolved and is not a software problem.**
  Every available software mitigation is applied and measured ineffective.
- **Test-boot every new image on a spare card.** A boot-layout bug in this repo's history
  produced several un-bootable images.
