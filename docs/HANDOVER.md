# Handover

**Read this first if you have inherited NetBridge and cannot ask the previous maintainer.**

The repository contains everything needed to build, flash, run and understand the system.
It does **not** contain the credentials — deliberately, because they are secrets. Five of
them live outside git, and without each one a specific capability is simply gone.

This page lists exactly what must be transferred, what breaks without it, and what you can
still do in the meantime.

---

## 0. Your right to use this

This project is **proprietary — © 2026 Samith Reddy, all rights reserved** (see
[LICENSE](../LICENSE)). Inheriting the repository does not by itself grant you the right to
use it. You need written authorisation from the owner, and it can be withdrawn.

Treat OS images as confidential: each one carries a fleet enrolment token, so anyone holding
an image file can add a device to the fleet.

---

## 1. What must be handed over

| # | Item | Lives at | Without it you lose |
|---|---|---|---|
| 1 | **Script signing key** | `~/.netbridge/keys/script-signing-key.pem` | **The ability to fix any already-flashed bridge remotely.** Irreplaceable — see below. |
| 2 | App signing key | `~/.netbridge/keys/app-signing-key.pem` | Signed presenter-app updates |
| 3 | Fleet automation token | `~/.netbridge/fleet-automation-token` | `tools/publish-script.sh` cannot upload payloads |
| 4 | Fleet host SSH key | `~/.ssh/netbridge-fleet.pem` | Deploying the control plane (`deploy.sh`) |
| 5 | Alert mailbox app password | `~/.netbridge/smtp-app-password` | Fleet email alerts |

Plus these accounts:

| Account | Why | Note |
|---|---|---|
| GitHub repo | Source, releases, CI | The repo is **private** |
| Fleet admin login | Claim bridges, run actions | Ask an existing admin to invite you at `https://fleet.scine.online` |
| Tailscale tailnet | The mesh every media leg rides | Bridges and presenters are nodes on it |
| AWS Lightsail | Hosts the control plane | Static IP `100.29.201.7`, ~$7/mo |
| `BOOTSTRAP_TOKENS` | In `/opt/netbridge/.env` on the fleet host | Baked into OS images so cards self-enrol |

---

## 2. ⚠️ The one that cannot be recreated

**`script-signing-key.pem` is the only key that can sign a fix for a bridge that is already
in the field.**

Every bridge verifies remote script payloads against `script-pubkey.pem`, which is baked
onto its **read-only root** at flash time. That check runs at install *and* at every service
start. It is the reason a hostile payload cannot run — and the reason that losing the
private key is unrecoverable for existing hardware.

If it is lost:

- Every already-flashed card **permanently refuses all future remote fixes**. They keep
  working; they just cannot be repaired from a distance again.
- You can generate a new keypair, but the new public key only reaches a bridge by
  **reflashing its SD card in person**.

**Back it up somewhere that is not one laptop.** It is 227 bytes.

---

## 3. What you can do before any of that arrives

You are not blocked on the basics. With only the repo and a fleet login you can:

- Build an OS image (`gh workflow run build-image.yml`) and flash a card
- Bring a bridge online through the phone setup portal and claim it
- Download and run the presenter app from Releases, and stream
- Run every read-only fleet action: diagnostics, logs, what-code-is-it-running
- Run `bash tools/preflight.sh` before a session
- Run both test suites (`tests/`) with no hardware attached

You are blocked only on: pushing signed script fixes, deploying the control plane, and
rotating enrolment tokens.

---

## 4. First week, in order

1. **Read** [README.md](../README.md), then [GOLDEN-RULES.md](GOLDEN-RULES.md). The golden
   rules were learned by breaking live calls; several look arbitrary until you know why.
2. **Flash a spare card** from the newest image and boot it. Never flash the only working
   card — a boot-layout bug in this repo's history produced five un-bootable images.
3. **Claim it** on the fleet and go live once. Confirm all five checks green.
4. **Pull a diagnostics bundle** while it is healthy and keep it. It is your baseline; every
   later "is this normal?" question is answered by diffing against it.
5. **Read** [CHANGELOG.md](../CHANGELOG.md) to see which states were verified against real
   hardware, and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the failures that actually
   happen.

---

## 5. Things that will mislead you

These are real, documented, and each one has cost somebody hours.

| Symptom | What it is not | What it is |
|---|---|---|
| Panel shows `throttled: 0x0` | Healthy power | The live field reads 0x0 even mid-brownout on older cards. Trust `power.rate` or a bundle's `power.txt`. |
| Audio stutters, network looks fine | A network fault | Under-voltage. The SoC throttles, so packets arrive **late, not lost** — 0% loss with high jitter. |
| `restarts: 0/0/0` | Proof it never rebooted | Per-boot counters that reset with the machine. Watch `uptime` going *backwards*. |
| Video dead, voice fine | The mesh | The presenter's camera is open but delivering no frames. Check the encoder's CPU delta. |
| Green "legs ok" while nothing works | Media flowing | On builds before v1.1.3 that only proved a socket was bound. BridgeWatch asks the bridge now. |
| A bridge suddenly unreachable at a new site | Power | A Wi-Fi **name** mismatch. It raises its setup hotspot; somebody must be on site with a phone. |

**Under-voltage on the reference hardware is unresolved and is not a software problem.**
Every software mitigation is applied and measured ineffective. Mitigate audibly by raising
the return jitter buffer (`POST /api/return-tuning {"jitter_ms":400}`); it treats the
symptom, not the cause.

---

## 6. Where the truth lives

When a claim and the system disagree, believe these:

- **What GitHub holds** — `gh api .../contents/<path>?ref=<branch>`. Local git commands have
  given false readings in this project (`git log @{u}..HEAD` once reported 0 unpushed while
  2 commits were unpushed).
- **What the bridge measures** — `/api/checks` reports feeder CPU ticks and the return
  stream's ALSA hardware pointer. Bytes moving, observed at the far end.
- **A diagnostics bundle** — for power, `power.txt` and `flight.txt`, never the live field.
- **A running binary** — `strings` cannot see inside a PyInstaller archive. To know what a
  build contains, run it and query `/api/state`.
