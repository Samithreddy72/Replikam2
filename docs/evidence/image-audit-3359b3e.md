# Image audit — netbridge-os-2.0.0-3359b3e

**Built** 2026-08-13 14:16 UTC from `3359b3e` · kernel `6.12.93+rpt-rpi-v8`
**Audited** 2026-08-13, before any flash. Nothing below is inferred from the commit; every
content check was read out of the image's own filesystem.

```
sha256   6127c8eddfc75a299f1b1f7386798004be609e7a9a2ed1e7b51b7d1b6e53634f
size     1,342,365,188 bytes   (exact manifest match)
manifest signature Verified OK — before AND after copying to the Desktop
```

---

## The download lied first

`gh release download` **exited 0 having written 262,144,000 of 1,342,365,188 bytes.** A
truncated image with a success exit code. Retrying with `curl -C -` then appended a second
full copy onto the stump and produced 1,346,219,524 bytes — larger than the real file, and
equally useless.

Only a size-and-hash check against the manifest caught either one. **Never treat a download
as complete because the tool exited 0**; the manifest exists precisely so the artifact can
be checked rather than trusted.

## Structure

```
MBR signature 0xAA55 · 4 partitions
  1  FAT32  bootable       2048  +1048576
  2  Linux  (A root)    1050624  +8388608
  3  Linux  (B root)    9439232  +8388608
  4  Linux  (/data)    17827840   +532480
```

Raw size 9,400,483,840 bytes. A/B roots both present, `/data` separate — so a fresh flash
wipes PIN, golden baseline and Wi-Fi, as intended.

## Content, read from partition 2

All 22 checks passed.

| Carried from the `bfa7336` restore point | |
|---|---|
| `echo 48000,44100,32000 > functions/uac2.usb0/c_srate` | ✓ |
| `format=S16BE ! rtpL16pay` | ✓ |
| `${aec_filter}opusenc` (no `! !`) | ✓ |
| `RETURN_ALLOWED_RATES:-32000 44100 48000` | ✓ |

| New, and never yet run on hardware | |
|---|---|
| `def throttle_sources():` — multi-source power reader | ✓ |
| `flight-sticky` — the recorder as a power source | ✓ |
| `def _valid_mask(s):` — reject text that is not a reading | ✓ |
| `bridge-golden.py` body + `/data/golden-profile.json` + `NEEDS DEPLOY` | ✓ |
| `bridge-jitter.py` body + ladder rungs + `/data/presenter-tuning.json` | ✓ |
| `def presenter_tuning():` and `"presenter_tuning": presenter_tuning()` in `/api/checks` | ✓ |
| `live: raised presenter buffer to rung` — the sentry's live-safe path | ✓ |
| `"by": "auto" if auto else "operator"` — ownership | ✓ |
| `an operator set rung %s by hand; not overriding` | ✓ |
| agent `jitter-diagnose` / `golden-save`; CLI `jitter` / `golden` usage strings | ✓ |

13 systemd units confirmed present, including `jitter-sentry`, `bridge-crackle-sentry` and
`flight-recorder`.

### A false MISSING, twice, from the same cause

`--auto` first reported absent. `grep -F "--auto"` parses the leading `--` as an option
terminator, so the pattern never reached the matcher. With `grep -F -e "--auto"` it appears
**839 times**. The identical trap produced a false MISSING on `--unquarantine` in an earlier
audit. Any pattern beginning with `-` needs `-e`.

## Secrets

Enrolment works, and the things that must never ship do not:

| | |
|---|---|
| `script-pubkey.pem` (public, must be present) | ✓ present |
| script-signing **private** key | ✓ absent |
| app-signing key | ✓ absent |
| fleet automation token | ✓ absent |
| SMTP app password | ✓ absent |
| real `tskey-auth-…` / `gh*_` / AWS keys | ✓ none |
| `BOOTSTRAP_TOKEN=` | **present by design** |

The bootstrap token is intentional and documented: anyone holding an image file can enrol a
device into the fleet, so **images are confidential**.

### Three false alarms, resolved by reading context

- `BEGIN EC/RSA/PRIVATE KEY` — 82 hits, all library string tables (`"PEM key file had no
  start tag"`), cloud-init documentation examples (`YOUR-ORGS-VALIDATION-KEY-HERE`), and
  OpenSSL/GOST test vectors.
- `AKIA[0-9A-Z]{16}` — matched inside a Unicode character-name table:
  `…INSIDE INWARD ISAKIA ISLAND IS SHARI TALIC…`.
- `tskey-auth` — the tailscale binary's `tskey-auth-%s` format string, plus our own docs and
  source (the repo is copied to `/opt/replikam2` at build time), all placeholders.

### And one false CRITICAL of my own making

The private-key check first printed `✗ LEAKED — CRITICAL`. That was a shell bug, not a
finding: `grep -c` exits 1 when it matches nothing, so `|| echo 0` appended a second zero,
`[ "0\n0" -eq 0 ]` errored, and control fell into the failure branch. **The `||`-fallback
idiom inverts any check whose good outcome is "not found".** Re-run without it: 0 matches,
key absent, correct.

## Tests at this commit

```
test-return-rate-follow.sh   20      test-power-readout.py     24
test-script-override.sh      23      test-golden-profile.py    16
test-gst-pipelines.sh         7      test-command-gates.py     16
                                     test-jitter-diagnosis.py  39
                                     test-tuning-relay.py      11
                                     ─────────────────────────────
                                     156 passed, 0 failed
```

## What this audit cannot tell you

Everything new here is verified **present and syntactically sound**, and nothing more. The
power fix, Golden Profile, jitter diagnosis, the relay and the live-acting sentry have never
executed on a Raspberry Pi. The post-flash checklist in
`~/Desktop/NetBridge-Image/WHATS-IN-THIS-IMAGE.txt` exists to close that gap, and the last
step is still **listen**.
