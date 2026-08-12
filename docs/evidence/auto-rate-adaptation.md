# Does the bridge adapt automatically when the MEETING changes rate?

**Yes — and the trigger is irrelevant to the mechanism.** This is the answer to "we tested by
changing the format by hand, but in a meeting it changes on its own — is that handled?"

## Why the trigger cannot matter

The bridge never learns *who* changed the rate. It watches the **ALSA device's own rate**:

    bridge-return-audio.sh — mismatch watchdog
      while the pipeline runs:
        sleep 10
        live = host_rate()                    # reads the kernel's Capture Rate control
        if live != rate the pipeline opened at:
            publish a mismatch event, re-open at `live`

`host_rate()` reads what the USB host actually negotiated. A person changing Windows Sound
settings, Zoom renegotiating when a participant joins, Teams switching between media and
voice profiles — all produce the *same observable*: the device's rate changed. There is no
separate code path for a "meeting" change, so there is nothing that could work for a manual
change and fail for an automatic one.

**The manual test on 2026-08-12 exercised exactly this path.** See
`m3-rate-follow-verified-2026-08-12.md`: 48000 → 44100 → 32000, each followed with
`mismatch: None`, frame ratios tracking the clock.

## Two mechanisms, one as a backstop

1. **Event path** — the kernel signals a rate change; the pipeline re-opens promptly.
2. **Mismatch watchdog** — polls every 10s and reconciles regardless.

The watchdog exists because a mismatched pipeline **throws no error**: `u_audio` keeps the
old session's stream running when the host changes rate, so neither the crash path nor the
event path can catch it. Only comparing live-device against pipeline-opened-at can.

The interval was cut 30s → 10s after hardware testing, because some switches settled via the
watchdog rather than the event path and the user waited 15–20s. Worst-case adaptation is now
~10–12s; typically it is immediate via the event path.

## What the presenter hears

**Nothing changes at the presenter's end, at any meeting rate.** The bridge resamples
(`audioresample quality=10`) and encodes Opus at a fixed 48 kHz, so the Mac always receives
48 kHz regardless of what the meeting is doing. Verified: at 44100 and 32000 the Mac player
kept decoding with no reconfiguration.

## The one real cost

At the instant of a switch the host closes the capture stream to renegotiate, so
`return_audio` reports BAD — `capture stream not open` — for a few seconds before recovering
unaided. **A rate change eats roughly a word of room audio.** Unavoidable: a live capture
cannot change rate without reopening. Not a fault, but it should not surprise anyone
mid-meeting.

## Prerequisite that is easy to lose

Auto-adaptation only has something to adapt *to* if the gadget advertises several rates:

    c_srate = 48000,44100,32000

With a single-rate descriptor the host can only ever pick 48 kHz, the rate never changes, and
the follower — however correct — never fires. Every image built between 2026-07-31 and
2026-08-12 shipped single-rate, so **check `gadget-av.txt` first on any new card.**
