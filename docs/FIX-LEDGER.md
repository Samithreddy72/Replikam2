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
| **P1-1** | queued commands cannot be cancelled — a real `reboot` reached live hardware during the audit and could not be recalled | HIGH | no `DELETE`; no command state machine | `DELETE /devices/{id}/commands/{cmd_id}`: `pending`→`cancelled` for real; `sent`→**409, refuses to lie** (the device already has it); terminal states immutable | 27 (`test-command-lifecycle.py`) | **required** | **SOFTWARE VERIFIED** |
| **P1-2** | commands sit in `sent` forever — observed >1 h while later commands completed | HIGH | no deadline, no retry policy, no dead-letter | per-class `TIMEOUT_S` table + `DEFAULT_TIMEOUT_S=240`; `sent_at` stamped on delivery; `_sweep_expired()` moves overdue to `expired` with a `fail_reason` | included above | **required** | **SOFTWARE VERIFIED** |
| **P1-3** | OTA rollback never exercised on hardware | HIGH | never tested | procedure written (audit §24.1); needs a spare Pi | — | **required** | **OPEN — HARDWARE** |
| **P1-4** | USB unplugged is indistinguishable from bridge offline | HIGH | one `udc: not attached` state covers five causes | `usb_diagnosis()` returns 5 named states **plus UNKNOWN**, and carries `certain=False` when the hardware genuinely cannot tell | 16 (`test-fault-attribution.py`) | **required** | **SOFTWARE VERIFIED** |
| **P1-5** | wedged Mac camera reported as a bridge fault | HIGH | heuristic assumes "surviving leg = remote"; a wedged camera leaves the leg up with zero throughput | encoder CPU rate vs `CPU_FLOOR=0.02` → `LOCAL_CAMERA_FAULT` naming the Mac-side fix. Evidence: 0.23 s/56 s wedged vs 26.81 s/81 s healthy | 12 (`test-app-diagnosis.py`) | **required** | **SOFTWARE VERIFIED** |
| **P2-3** | `update` (OTA trigger) has no UI, only an unlabelled API call | MED | never surfaced | Rollouts panel: staged waves 10→25→50→100%, admin-only controls, per-device auto-rollback and the "0 rollbacks" halt; `update` also gated by `CONFIRM_REQUIRED` | 26 (`test-command-gates.py`) | **required** | **SOFTWARE VERIFIED** |
| **P2-4** | hostname rename fails on hardware | MED | **read-only ext4 root, not an overlay** — and `hostnamectl` exits 0 while doing nothing, so the `\|\|` fallback never ran | `bridge-identity.sh` sets it via `sethostname(2)` and **reads it back to confirm**, writing through `/data` rather than the ro rootfs | 14 (`test-firstboot-identity.py`) | **required** | **SOFTWARE VERIFIED** |
| **P2-5** | golden baseline records a state never confirmed by ear | MED | written by an accidentally queued `golden-save` during the audit | save() records `by` / `confirmed` / `verified`, consults `_live_health()`, and **refuses to overwrite a trusted baseline unconfirmed** | 19 (`test-golden-baseline.py`) | **required** | **SOFTWARE VERIFIED** |
| **P2-6** | destructive commands have no backend confirmation gate | MED | confirmation is UI-only | `CONFIRM_REQUIRED` rejected at the API with 400; panel mirrors the same set and sends `confirm:true`. Both levels — the contract test caught that an early version would have broken every destructive button | 26 (`test-command-gates.py`) | **required** | **SOFTWARE VERIFIED** |
| **P2-2** | tailnet key passed as a command-line argument on the Mac | MED | convenience | — | — | — | **DEFERRED** — operator excluded the tailnet work from this sprint |
| **P2-1** | tailnet key expiry unmonitored | MED | no alerting | — | — | — | **DEFERRED** — same |
| — | crackle sentry | — | ships disabled; never caught a real crackle | — | — | — | **DEFERRED** — operator excluded it from this sprint; stays installed and off |
| **P3-1** | Windows app not built | LOW | Chocolatey GStreamer checksum fails upstream | not papered over with `--ignore-checksums`: GStreamer is the only Windows return-audio player, so that build would look fine and have no room audio | — | — | **OPEN — UPSTREAM** |
| **P3-2** | video quality/latency never measured | LOW | only arrival is checked | `video_throughput()` computes observed vs `_expected_fps()` from real frame bytes, so a black or frozen picture is distinguishable from a healthy one | 10 (`test-video-health.py`) | **required** | **SOFTWARE VERIFIED** |
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

---

## Audit of 25 August 2026, evening — corrections to THIS file

This ledger was itself audited, and it was wrong. It is the fourth artefact in this sprint to
claim a state that the code did not support, so the corrections are recorded rather than
quietly edited away:

- **Eight rows said OPEN for work that was finished.** P1-1, P1-2, P1-4, P1-5, P2-3, P2-4, P2-5
  and P2-6 all shipped in later phases with tests, and the table still described them as
  "planned". A ledger that under-reports is as misleading as one that over-reports: it invites
  the same work to be done twice and hides what actually needs hardware time.
- **P3-2 was closed without being listed as closed** — video health landed with the P1-4 work.
- **The verification column was the useful part and stays honest.** Every row above now reads
  SOFTWARE VERIFIED, and *not one* reads HARDWARE VERIFIED. Nothing in this sprint has run on a
  bridge. That is the single most important fact in this file and the reason the final image
  is still unflashed.

### Test-suite integrity

`tests/test-app-diagnosis.py` **had not executed a single assertion for two phases**. The
Phase-4 change added `SESSION.leg_cpu_rate()` to the method it lifts out by regex and `exec`s,
so the file died on `NameError` before reaching its own summary line — and the ad-hoc runner
counted a file with no summary as *no result* rather than a failure, so the total simply came
out 12 lower and nobody looked.

Fixed on both sides: the test gets a `SESSION` stand-in (and now covers the CPU branch it was
blind to), and `tools/run-tests.sh` treats **no summary line as a FAILURE**. A test that cannot
fail loudly is not a test.

True suite size: **385 passing**, 0 failing, 0 skipped, 0 without result — up from a reported
346, of which 12 were dead and 27 were being skipped for a missing virtualenv.
