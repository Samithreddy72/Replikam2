# NetBridge — fix ledger

Findings from the production audit of 25 August 2026 and their state. Nothing is removed from
this file; entries only change status.

**Status vocabulary** — these are not interchangeable:

```
OPEN                 not started
IN PROGRESS          being worked
CODE FIXED           the change exists; nothing has run it
SOFTWARE VERIFIED    regression tests prove the change behaves as intended
HARDWARE VERIFIED    exercised on a real bridge
PRODUCTION VERIFIED  exercised during a real meeting
DEFERRED             deliberately postponed, with a reason
```

A fix is never described as "done". It has a status.

---

## Baseline — do not overwrite

```
git             e29c1bb   tag baseline-pre-fixsprint-2026-08-25
image           2.0.0-1db20ec   sha256 b884de3a…a17989   signature Verified OK
app             1.1.9
control plane   deployed 24 Aug 2026
tests           258 passing
image audit     86 checks
app audit       30 checks
score           61 / 100 — NOT READY FOR MULTI-ROOM
```

---

## Ledger

| ID | finding | sev | root cause | fix | tests | hardware test | status |
|---|---|---|---|---|---|---|---|
| **P0-1** | `POST /api/set-peer` unauthenticated — any LAN host can redirect the room's microphone | CRITICAL | `bridge-web` had no auth on any endpoint | mutations require loopback or 100.64.0.0/10; 403 + journalled with source; check at the top of `do_POST` so new endpoints inherit it | 28 (`test-bridge-web-auth.py`) | **required** — not on any card | **SOFTWARE VERIFIED** |
| **P3-4** | `shell=True` in the network-facing process | LOW | convenience | argv via `shlex.split`; redundant `2>/dev/null` removed from 5 callers; `sh()` refuses shell-needing commands loudly | included above | with P0-1 | **SOFTWARE VERIFIED** |
| **P1-1** | queued commands cannot be cancelled — a real `reboot` reached live hardware during the audit and could not be recalled | HIGH | no `DELETE`; no command state machine | planned: cancel where safe (`queued`, `sent` pre-ack), never fake a cancellation the device will still execute | — | — | **OPEN** |
| **P1-2** | commands sit in `sent` forever — observed >1 h while later commands completed | HIGH | no deadline, no retry policy, no dead-letter | planned: per-class timeouts (read-only short, reboot long, OTA deployment-specific), retry count, dead-letter with reason | — | — | **OPEN** |
| **P1-3** | OTA rollback never exercised on hardware | HIGH | never tested | procedure written (audit §24.1); needs a spare Pi | — | **required** | **OPEN — HARDWARE** |
| **P1-4** | USB unplugged is indistinguishable from bridge offline | HIGH | one `udc: not attached` state covers five causes | planned: distinguish `BRIDGE_OFFLINE` / `BRIDGE_ONLINE_USB_DISCONNECTED` / `USB_HOST_NOT_ENUMERATING` / `USB_GADGET_ERROR` / `USB_CONNECTED_HEALTHY`, and say UNKNOWN where the hardware cannot tell | — | — | **OPEN** |
| **P1-5** | wedged Mac camera reported as a bridge fault | HIGH | heuristic assumes "surviving leg = remote"; a wedged camera leaves the leg up with zero throughput | planned: encoder CPU rate + frame progress → `LOCAL_CAMERA_FAULT`. Evidence: 0.23 s/56 s wedged vs 26.81 s/81 s healthy | — | — | **OPEN** |
| **P2-3** | `update` (OTA trigger) has no UI, only an unlabelled API call | MED | never surfaced | planned | — | — | **OPEN** |
| **P2-4** | hostname rename fails on hardware | MED | read-only overlay; `hostnamectl` appears to succeed while doing nothing | needs the correct persistent-hostname design for an overlay root, then hardware iteration | 8 (ordering only) | **required** | **OPEN — HARDWARE** |
| **P2-5** | golden baseline records a state never confirmed by ear | MED | written by an accidentally queued `golden-save` during the audit | planned: explicit operator-confirmation workflow + metadata (`audio_verified`, `operator_confirmed`, versions); refuse silent overwrite of a trusted baseline | — | — | **OPEN** |
| **P2-6** | destructive commands have no backend confirmation gate | MED | confirmation is UI-only | planned: backend rejects unconfirmed destructive requests; UI confirms as well — both levels | — | — | **OPEN** |
| **P2-2** | tailnet key passed as a command-line argument on the Mac | MED | convenience | — | — | — | **DEFERRED** — operator excluded the tailnet work from this sprint |
| **P2-1** | tailnet key expiry unmonitored | MED | no alerting | — | — | — | **DEFERRED** — same |
| — | crackle sentry | — | ships disabled; never caught a real crackle | — | — | — | **DEFERRED** — operator excluded it from this sprint; stays installed and off |
| **P3-1** | Windows app not built | LOW | Chocolatey GStreamer checksum fails upstream | not papered over with `--ignore-checksums`: GStreamer is the only Windows return-audio player, so that build would look fine and have no room audio | — | — | **OPEN — UPSTREAM** |
| **P3-2** | video quality/latency never measured | LOW | only arrival is checked | planned: latency, drops, cadence, decode failures | — | — | **OPEN** |
| **P3-3** | echo cancellation | LOW | — | — | — | — | **DEFERRED** by the operator, long-standing |

---

## Jitter — deliberately not in the table

Jitter is **unresolved** and is not a line item to close. The audit retired the Wi-Fi theory
(the two most recent clean sessions ran at *worse* signal than a jittery one). The only
surviving correlation is live brownout rate, at **n=1** on the failing side, because five of
six jittery snapshots were captured on an image whose power readout was broken.

The next step is instrumentation, not a fix: capture the next episode on a power-capable image
with synchronised samples, then correlate. Do not change the architecture on the strength of
the current evidence.

The `req_number` 8-vs-32 A/B remains genuinely unanswered, and the outcome measure must be
**exact-zero runs in decoded audio**, not capture rate error — rate error measures the clock,
while `req_number` is tolerance to driver lateness.

---

## Rules being followed in this sprint

- The known-good baseline is preserved and tagged; nothing overwrites it.
- No destructive fleet command is executed against production to test itself.
- Software tests never promote something to hardware-verified.
- Correlation is never written down as root cause.
- Every fix leaves a regression test behind.
- Where the system cannot determine a fault location, it must say UNKNOWN rather than blame
  another component.
