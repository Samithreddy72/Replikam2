# The fleet page

Every bridge on one screen, and fixable from it. Live at **`https://fleet.scine.online`**.

Sign in with your work email — a magic link arrives, no password exists. You see only
bridges in your organisation, and every action you take is recorded in the audit log with
your name on it.

---

## Reading a row

```
🆕 Unclaimed · BRIDGE-2626     online    —     dev    just joined    [Check…] [Claim]
TestBridge 2626       ● live  direct   PIN armed  dev  presenter streaming  [Actions…]
```

| Column | Meaning |
|---|---|
| **Bridge** | Your name for it, plus the pairing code printed on its label |
| **Status** | `online` · `● live` (someone is streaming) · `offline Nd` · or an active alert |
| — `direct` / `relayed` | **How the presenter is reached.** Direct is a punched peer-to-peer path; relayed goes through a Tailscale DERP server and roughly doubles latency. On a long link this is the most useful thing on the page. |
| — `deployed code not running` | Auto-rollback has parked an override and the bridge is silently running its **factory** script. See [REMOTE-RECOVERY.md](REMOTE-RECOVERY.md). |
| **PIN** | `PIN armed` means a presenter needs it. `locked out` means three wrong tries. |
| **Session** | Who is streaming, if anyone |

**A newly joined bridge sorts to the top** as `🆕 Unclaimed · just joined`, because that is
the moment somebody is watching for it.

---

## Actions, grouped by risk

The menu is ordered safe-first, and labelled by **symptom** rather than command name — at
2am you know what you are hearing, not what it is called.

### Look first — changes nothing

| Action | Use it when |
|---|---|
| **Collect diagnostics** | You need the full picture: services, logs, USB state, power history, audio analysis. ~470 KB bundle. |
| **What code is it running?** | You need to know whether a bridge is on an override or its factory script — with hashes. |
| **Fetch recent logs** | You want "what just happened", not forensics. Bounded tail, much lighter than a bundle. |

These are available on **unclaimed** bridges too, so you can inspect one before adopting it.

### Repair — fixes a known fault

| Action | Fixes |
|---|---|
| **Audio robotic or crackling → reset audio clock** | Capture rate drifted from the pipeline rate |
| **Return audio going nowhere → re-issue mesh key** | The presenter's mesh node changed and the bridge is pointed at a stale address |
| **Deployed code not running → restore override** | Auto-rollback parked your code. Re-verifies the signature before restoring, and gives it a fresh trial. |
| **Undo a bad script → revert to factory…** | A deploy made things worse. Needs no URL. |
| **Push a code fix → deploy signed script…** | Ship a fix to a bridge anywhere. Publish it first with `tools/publish-script.sh`. |

### Access — no effect on media

Set or rotate a PIN, clear a lockout after three wrong tries, or lock a bridge so it demands
one. PINs travel offline — the panel never shows one after you set it.

### Interrupts the stream

**Restart media**, **jitter profile LAN/WAN**, **reboot**. Each confirms before running, and
warns harder when someone is streaming.

> A jitter-profile change restarts the video and audio pipelines. It belongs in this group —
> it once tore down a live meeting because a background sentry triggered it automatically
> when the network had been *good* for ten minutes.

---

## Alerts

Alerts appear with their known fix attached, and also email you. Seven kinds: offline,
thermal, under-voltage/throttling, service failure, restart storms, the audio-clock crackle
signature, and PIN lockout.

An alert clears itself when the condition does.

> ⚠️ **Do not judge power health from the `throttled` field.** It reports the *instantaneous*
> value and reads `0x0` even on a bridge that has been browning out all day. Only
> `power.txt` and `flight.txt` inside a diagnostics bundle carry the sticky bits.

---

## Diagnostics bundles

**Actions → Collect diagnostics** runs on the device and uploads the result; download it from
the card on the right. Inside:

| File | What it answers |
|---|---|
| `services.txt` | Is every unit up, and which binary is each one running |
| `journal-tail.txt` | What the device has been saying |
| `power.txt`, `flight.txt` | **The truth about power** — sticky under-voltage bits and a per-second history that survives reboots |
| `mesh.txt` | Tailscale peers and whether each path is direct or relayed |
| `gadget-av.txt` | USB gadget state, offered sample rates, who has the video device open |
| `media-health.txt` | Restart counters — climbing means a crash loop |
| `clock-fft.txt` | Spectral analysis of the captured audio, with a crackle verdict |
| `network.txt` | Wi-Fi band, signal, negotiated rate |

**Reading a reboot:** service restart counters are **per-boot** and reset with the machine —
they cannot tell you a bridge rebooted. Watch `uptime` going *backwards* instead.

---

## Team

Invite people by email with a role. Admins manage the fleet; presenters can stream but
cannot change fleet settings. Revoke access from the same panel.

---

## Adding a new action

A command must pass **three** gates or it fails:

1. `ACTIONS` in `control-plane/panel-dist/index.html` — the button
2. `ALLOWED_COMMANDS` in `control-plane/backend/app/main.py` — or the API rejects it with
   `unsupported command type` before it is queued
3. `ALLOWED` in `pi/scripts/bridge-agent.py` — or the device refuses it

Miss the middle one and the button appears, looks correct, and fails on first click.

Add it to `DISRUPTIVE` too if it can interrupt a stream.
