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

## Actions, grouped by symptom

You arrive with a **symptom**, never a diagnosis, so the menu is grouped by what you can
observe. Every entry that interrupts anything says so in its own label (`⚠`) and confirms
before running — harder when someone is streaming.

### The distinction the whole menu rests on

> **The audio YOU hear from the room is buffered on YOUR machine.**
> **The audio THE ROOM hears from you is buffered on THE BRIDGE.**

They need opposite actions. The old label *"Network rough → jitter profile WAN"* read as
though it fixed what you were hearing. It cannot, ever — it tunes the other direction, and
costs a five-second video freeze to do it. Two groups now keep them apart.

### Not sure what is wrong? Start here

| Action | Use it when |
|---|---|
| **Diagnose it for me** | Anything audio-related. Measures power, path, sample rate, running pipeline, mesh path and config drift, then **names the culprit and offers only the action that treats it**. Changes nothing. |
| **What code is this bridge running?** | You need to know whether it is on an override or its factory script — with hashes. |
| **Recent logs** | "What just happened", not forensics. |
| **Full diagnostics bundle** | The whole picture. ~40 s, ~470 KB. |

### You cannot hear the ROOM properly

| Action | Fixes | Cost |
|---|---|---|
| **Choppy → deeper buffer on your side** | Lateness from any cause: network bursts, or a browning-out board | ~1 s of room audio. **Video untouched.** |
| **Still choppy → deeper still** | The same, more of it | ~1 s of room audio |
| **Silent — nothing at all → re-issue mesh key** | The presenter's mesh node changed and the bridge points at a stale address | none |
| **Sounds right again → hand the buffer back** | Returns to the 250 ms default | ~1 s of room audio |

The buffer being raised is **on the presenter's laptop**. The fleet cannot reach a laptop
behind NAT, so the bridge publishes the request and the app adopts it on its next 10-second
poll. Values are clamped app-side (60–1000 ms): the bridge is a courier, not an authority.

### The ROOM cannot hear YOU properly

**Deepen / shallow the bridge buffer.** ⚠ ~5 s video freeze — it restarts the feeders.

### Robotic, crackling, or devices missing in the meeting

**Reset the audio clock.** ⚠ The meeting laptop's camera, microphone **and** speakers drop
and must be re-selected there. Right for a degraded UAC2 clock, harmful for anything else —
so let **Diagnose** confirm `clock_suspect` before reaching for it.

### Nothing is arriving at all

**Restart media** (⚠ ~5 s video freeze) or **reboot** (⚠ ~30 s outage).

### Baseline — what "working" looked like

| Action | Use it when |
|---|---|
| **Sounds good right now → save as known-good** | Immediately after you have verified audio by ear. Snapshots the jitter profile, return tuning, AEC, the gadget's advertised rates and the media scripts' hashes. |
| **Config drifted → restore known-good** | The **Config** column says `drift N`. ⚠ may briefly freeze video. |

Restore fixes only the **runtime tunables**. The advertised sample rates and script hashes are
recorded as a fingerprint and **reported, never restored**: `c_srate` is rewritten at every
boot by `uvc-raw-setup.sh` on the read-only root, so poking configfs would be undone on reboot
while looking exactly like a repair that worked. Those show as **NEEDS DEPLOY**.

### Code on the bridge

**Restore override**, **revert to factory**, **deploy signed script**. Each ⚠ restarts the
affected service.

### Access — no effect on media

Set or rotate a PIN, clear a lockout, or lock a bridge. PINs travel offline — the panel never
shows one after you set it.

### Last resort

**Max buffer + WAN profile.** ⚠ ~5 s video freeze.

---

## The culprits, and what treats each one

Every row is a failure this system has actually produced. **Diagnose** detects all of them.

| Culprit | How it presents | Action |
|---|---|---|
| Presenter voice path down | The room hears nothing (the 2026-08-12 `S16LE` bug) | Restart media → revert-script if it will not stay up |
| Room audio path down | You hear nothing (the 2026-08-12 `! !` bug) | Restart media → revert-script |
| Degraded UAC2 clock | Crackle, clicks | Reset the audio clock |
| Laptop not playing into NetBridge | Everything green, no frames arriving | **None.** On the meeting laptop, select NetBridge as the **speaker**. Not the microphone. |
| No USB host | Gadget never reaches `configured` | **None.** Almost always a charge-only cable. |
| Deployed code not running | Auto-rollback parked your override | Restore override |
| Opus concealment ON | Sounds like jitter (the 2026-08-03 regression) | Deploy signed script |
| Sample-rate mismatch | Robotic, ~72 % speed | Restart media |
| Under-voltage | Stutter with **0 % packet loss** — late, not lost | Deeper buffer. **Masks it; the cure is electrical.** |
| Network bursts | Spread ≥ 8 ms, no loss | Deeper buffer |
| Packet loss | ≥ 2 % loss | **None.** A buffer cannot replace packets that never arrived. |
| Relayed mesh path | Roughly double latency | Re-issue mesh key |
| Config drift | Differs from the saved baseline | Restore known-good, when restorable |

Ordering matters: **silence outranks jitter** (no buffer improves a dead pipeline), and
**under-voltage outranks network bursts**, so you are never sent chasing the network for a
power fault.

---

## The sentry acts while you are live

`jitter-sentry` measures the path every 20 s. It will not switch jitter profiles during a
session — that freezes video — but it **will** raise the presenter's buffer, which does not.
Rung 1 after ~1 minute of trouble, rung 2 after ~3.

It can never overwrite a rung **you** set by hand, and it withdraws only its own changes, only
between sessions. A ping looking healthy is not evidence that you were wrong about what you
could hear.

---

## Alerts

Alerts appear with their known fix attached, and also email you. Seven kinds: offline,
thermal, under-voltage/throttling, service failure, restart storms, the audio-clock crackle
signature, and PIN lockout.

> **The crackle alert needs its detector switched on.** `bridge-crackle-sentry` ships but is
> **not enabled** as of 2026-08-24: it had never caught a real crackle, and during an audio
> fault nobody has explained yet, every extra service in a live session is another variable.
> Until it is enabled deliberately, `clock_suspect` stays false and that alert cannot fire.

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
