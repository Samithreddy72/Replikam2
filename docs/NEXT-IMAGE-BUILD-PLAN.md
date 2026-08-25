# Next image build — the authorized-phase package

**Not yet built. Not yet flashed. This is the plan the next phase executes.**

## 1. Build from

```
git sha        (whatever HEAD is when authorized — the gate enforces clean + synced)
VERSION        2.1.0            # from ./VERSION, no longer typed at dispatch
image version  2.1.0-<short>    # composed by CI
rollback       v2.0.0-1db20ec   # BEST KNOWN GOOD — must remain published, untouched
```

## 2. Gate first — it is machine-checkable

```bash
bash tools/pre-build-gate.sh      # exit 0 = clear; 1 = fail; 2 = cannot verify (also stop)
```

Fourteen gates: clean tree, HEAD == origin/main, VERSION present and used, release tag bound
to the built commit, repo copy stripped from the image, bootstrap token 0600, sweep polices
it, image records provenance, no tracked databases, no credential values, full suite green
with zero NO-RESULT, and the known-good fallback still published with its assets.

**CANNOT VERIFY exits non-zero.** An unevaluated gate is not a passed gate.

## 3. Build — once

```bash
gh workflow run build-image.yml --ref main
```

Do not build, adjust, and rebuild. If something needs changing, the gate runs again from the
top and the previous run is cancelled, not shipped around.

## 4. What this image changes vs the known-good

| change | why it matters |
|---|---|
| **P0 mutation auth** (`182eaea`) | closes `set-peer`/`return-tune`/`unlock` to unauthenticated LAN callers — the reason this build exists |
| bootstrap token `0600` | the fleet enrolment token was world-readable on the card |
| repo copy stripped | ~50 MB of git history no longer ships into meeting rooms |
| `/etc/bridge/release.json` | the device can finally say which commit it is |
| USB/settling/diagnosis fixes | four bridge-side commits already on `main` |

## 5. Verify the artifact before it touches a card

```bash
bash tools/image-audit.sh <image.img>     # ~90 checks, includes the token-mode check
shasum -a 256 <image>.img.xz              # must equal sha256= in manifest-disk.txt
openssl dgst -sha256 -verify ota-pubkey.pem -signature manifest-disk.txt.sig manifest-disk.txt
```

The image audit must confirm **from the artifact** that `/etc/default/bridge-agent` is `0600`
and that `_mesh_or_local` is present in the shipped `bridge-web.py`. Do not accept the
builder's word for either.

## 6. Post-flash validation — in this order, non-destructive first

```
 1  confirm no session is live
 2  keep the known-good card; do not overwrite it
 3  flash, boot, wait ~90s
 4  bash tools/fleet-drift-check.sh <ip>          -> MUST exit 0
 5  curl http://<ip>:8080/api/status              -> build.git_sha == the built commit
 6  POST /api/set-peer from the LAN with a bad IP -> MUST return 403, not "bad ip"
 7  5/5 services active, udc=configured
 8  USB: laptop sees the camera and the audio device
 9  video arrives, voice arrives, return audio advances at the meeting's rate
10  one controlled meeting
11  bash tools/session-end-check.sh
12  bash tools/fleet-drift-check.sh <ip>          -> still 0
```

Step 6 is the one that proves the point of the whole exercise. It is the same harmless probe
that exposed the gap: an invalid IP, so the outcome is diagnostic either way.

## 7. Rollback

`v2.0.0-1db20ec` stays published, with its assets and hashes untouched, until the new image is
**built, audited, signed, hardware-verified and production-verified**. Until then it remains
the only image proven on hardware.

## 8. Known limitations that this image does NOT fix

- Mac distribution still needs an Apple Developer account
- Windows still has no hardware validation
- OTA rollback still has no hardware evidence
- Meeting-laptop behaviour across Zoom/Teams/Meet is still unmeasured
- Deferred by operator: Tailscale expiry, tailnet key in argv, jitter, hostname/multi-bridge,
  noise sentry
