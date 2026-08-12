# M3 — return audio follows the meeting rate: VERIFIED ON HARDWARE

**2026-08-12** · bridge `Scine Test` / `BRIDGE-2626` · the multi-rate card ·
presenter live throughout · measured through the fleet, not inferred.

Closes the walkthrough promise **J3 phase 6**, which the 21 July build ledger listed as
`PENDING · needs ears + rig`.

## What was measured

The Windows host's output format was changed live, twice, while streaming. `hw_ptr` is the
ALSA **hardware pointer** on the bridge's UAC2 capture device — it counts frames the
hardware actually clocked, so it cannot be faked by a reported setting.

| Host format | frames / 2s | implied rate | ratio vs 48k | expected |
|---|---|---|---|---|
| 48000 Hz | ~102,000 | ~50,976/s | 1.000 | 1.000 |
| 44100 Hz | 93,271 | ~46,635/s | 0.914 | 0.919 |
| 32000 Hz | 68,512 | ~34,256/s | 0.672 | 0.667 |

Ratios track the clock. The hardware genuinely runs at each rate.

At every step: `return_mismatch = None` (pipeline and device agree), video and voice gates
stayed green, and **no systemd restarts**. The presenter's Mac continued receiving 48 kHz
Opus — the bridge resamples, so nothing on the presenter side adapts.

## The descriptor that makes it possible

From the card's own diagnostics bundle (`docs/evidence/working-card-gadget-av-2026-08-12.txt`):

    == UAC2 gadget ==
    c_sync  = adaptive
    c_srate = 48000,44100,32000

Windows only offers formats the device advertises. A single-rate descriptor gives the user
no choice — which is exactly what the regressed images shipped.

## Why the 2026-07-31 revert did not apply

That commit concluded multi-rate "is not viable on this controller", from:

    known-good, no supervisor  :  0.2 zero-runs/sec
    multi-rate + supervisor    :  1.4 /sec
    single-rate + supervisor   : 21.6 /sec

Multi-rate was **only ever measured with the v1 supervisor**, and the same commit's first
conclusion was that the supervisor is the dominant fault — a shell loop restarting gst under
a live ALSA capture, 100x worse than not having it. The 1.4 figure was never isolated from
that damage. Phase 6 v2 replaced the supervisor with kernel Capture-Rate control (following
*inside* the pipeline, which is what the revert said it required), and the descriptor was
then deployed by card surgery. This is that configuration, finally measured and committed.

## Known cost of a switch

`return_audio` goes BAD for a few seconds at the moment of the change —
`capture stream not open` — then recovers unaided. That is the host closing the stream to
renegotiate, not a fault. It means **a rate change costs a brief audible gap**, roughly a
word. Unavoidable: a live capture cannot change rate without reopening.

## How to re-verify on any future card

1. Confirm the descriptor: `diagnose` → `gadget-av.txt` → `c_srate` must list all three.
   If it shows only 48000, the image regressed — check `pi/scripts/uvc-raw-setup.sh`.
2. Go live, then change the host's output format between 48000 / 44100 / 32000.
3. After each change, read `/api/status` (`return_rate`, `return_mismatch`) and
   `/api/checks` (`return_audio` detail carries the frame count).
4. **Pass** = rate follows, `mismatch: None`, frame ratio tracks the clock, gates recover
   within ~10s, no restarts.
5. Ears are still the final check — the instruments prove frames move, not that they sound
   right.

If audio degrades after a change to the media path, measure it the way the revert did:
capture the return stream to a file and count runs of EXACT ZEROS. 0.2/sec clean,
21.6/sec broken. **Transport metrics cannot see this** — the damage happens at the gadget,
before opus encodes.
