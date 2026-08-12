# Restore point — multi-rate audio verified on a freshly flashed card

**Tag:** `netbridge-MULTIRATE-VERIFIED-2026-08-13`
**Card:** `netbridge-os-2.0.0-bfa7336` · sha256 `b51506d3c998fec8b2fae8461612ccdcd80884532e4aa9dc527b9b0925375d9f`
**Presenter app:** `1.1.5` · binary sha `07588ad91b84c9b3…` · mesh sidecar `b180a4ff20787bfd…`

This is the first state in the project's history that is **both multi-rate and audibly clean**,
on a card flashed from a published image with **no signed override and no card surgery**.

Every earlier state had one or the other:

| | multi-rate | audio clean | flashed from an image |
|---|---|---|---|
| the old "working card" | yes | jitter at 48k, unexplained | no — hand-edited, never in git |
| `AUDIO-VERIFIED-2026-08-12` (`aaf2d28`) | **no** — 48000 only | yes | yes |
| **this** (`bfa7336`) | **yes** | **yes** | **yes** |

---

## 1. The card is genuinely fresh

Not an old card with new files on it — proven, because the distinction has misled this
project before:

```
dmesg      systemd[1]: Initializing machine ID from random generator   ← first boot, empty /data
journal    RegisterReq: ... machineAuthorized=true                     ← enrolled itself
fleet      claimed · PIN set · 0 alerts
```

> **Trap:** `systemctl status` on this card reports services "active since Thu 2026-06-18 …
> 1 month 25 days ago" on a machine that booted minutes ago. That is **not** a stale card.
> The Pi has no RTC: it boots at the image's build date, NTP jumps the clock forward, and
> systemd renders each start time as `now − monotonic_age`, landing two months in the past.
> Trust `/proc/uptime` (what the fleet's `uptime` field reports), never the rendered date.
> I called this bundle "stale" on first read and was wrong.

## 2. The gadget advertises all three rates

Read from the **running gadget** (`gadget-av.txt`), on two independent pulls:

```
c_srate = 48000,44100,32000
c_sync  = adaptive
```

This is the line that makes the feature exist: Windows only offers formats the device
advertises, so a single-rate descriptor gives the user no choice at all. It lived on **one
SD card and never in git** until `01971b1`, which is why every image built between
2026-07-31 and 2026-08-12 silently took the frequencies away when flashed.

## 3. Auto-adaptation, measured live, mid-meeting

Not a bench test and not a human editing config — the meeting application changed the host
format and the bridge followed, while a real call was in progress:

```
20:40:04  capture @ 48000 Hz -> peer:5004
20:43:44  mismatch watchdog — device at 32000, pipeline at 48000; re-opening
20:43:44  capture @ 32000 Hz
20:44:04  mismatch watchdog — device at 44100, pipeline at 32000; re-opening
20:44:04  capture @ 44100 Hz
20:44:24  mismatch watchdog — device at 48000, pipeline at 44100; re-opening
20:44:24  capture @ 48000 Hz
```

```
not-negotiated errors   0
link errors             0
crash loops             0
service restarts        0   (feeder_net 0 · uvcd 0 · return_audio 0)
```

**Audible verdict:** clear at all three rates, confirmed by ear by the operator. The
instrument measurement is `hw_ptr` (the ALSA hardware pointer — frames the hardware actually
clocked, which a reported setting cannot fake): **47,989–47,995 frames/s @ 48 kHz**, sampled
three times.

### Two findings inside that log

1. **Every switch came from the mismatch watchdog, not the event path.** The `alsactl
   monitor` events did not fire; the 10-second reconcile poll caught all three. Adaptation
   is therefore correct but takes ~10–20 s, which matches the operator's earlier report of
   waiting 15–20 s. The event path is for promptness, the watchdog for correctness — and
   right now only the watchdog is doing the work. Worth investigating; not a defect.
2. **`jitter-sentry` wanted to act and could not:**
   ```
   20:50:50  DEFERRED profile lan (network pristine for 10min) — a session is live
   20:51:13  DEFERRED profile lan (network pristine for 10min) — a session is live
   ```
   Correct behaviour (a profile switch freezes video), but it means the sentry does nothing
   during the exact meeting where jitter would appear. This is the premise of Plan D.

## 4. The exact configuration that sounds good

Read off the **running processes**, not from source:

| | |
|---|---|
| jitterbuffer | `rtpjitterbuffer latency=300` (WAN profile — `NET_VIDEO_LATENCY=300`, `NET_AUDIO_LATENCY=300`) |
| L16 reference branch | `format=S16BE ! rtpL16pay` — the RFC 3551 endianness fix |
| return decoder | `opusdec plc=true` — **no concealment flag** (the 2026-08-03 regression is absent) |
| return encoder | `opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20` |
| speaker sink | `alsasink device=plughw:UAC2Gadget sync=false buffer-time=200000 latency-time=40000` |
| room capture | `alsasrc device=hw:UAC2Gadget buffer-time=200000 latency-time=20000` (`hw:`, never `plughw:`) |
| allowed rates | `RETURN_ALLOWED_RATES="32000 44100 48000"` |
| mesh | direct path, `192.168.29.192:49170` — no relay |
| USB | high-speed · `udc configured` · `uac2` · `video40` |

## 5. Health at the moment of capture

```
gates        online ✓  video_arriving ✓  voice_arriving ✓  client_sees_camera ✓  return_audio ✓
mesh path    ping stddev 1.45 / 5.51 / 1.64 ms over three 20-packet runs, 0% loss
power        throttled=0x50000 (sticky: has browned out since boot) · brownout 0.00% of last 500 s
temp         46.7 → 56.0 °C under load
wifi         −41 dBm
```

A single preflight run reported "jitter is high (stddev 12 ms)". Three immediate re-runs gave
1.45 / 5.51 / 1.64 ms. **That was one Wi-Fi burst and a threshold too twitchy to survive a
single sample** — the buffer at 300 ms absorbs the worst spike seen (63 ms) eight times over.

## 6. Tests

```
tests/test-return-rate-follow.sh    20 passed, 0 failed
tests/test-script-override.sh       23 passed, 0 failed
tests/test-gst-pipelines.sh          7 passed, 0 failed, 2 skipped
```

`test-gst-pipelines.sh` was **rewritten for this restore point** and the change matters more
than the count. It previously reported a permanent FAIL on `bridge-return-audio.sh`, excused
as "a known false alarm the hardware disproves". That excuse was the problem: the harness
tried to recover pipelines by grepping source and substituting shell variables, which cannot
work for a pipeline assembled from seven of them — so the one file with a genuinely broken
pipeline was also the one file the suite could not see.

It no longer parses anything. It **runs each real script with a stub `gst-launch-1.0`**, so
the shell performs every expansion exactly as it does on the Pi, and constructs the resulting
argv with hardware elements faked. That covers three previously untested pipelines
(follower AEC-off, follower AEC-on, and the no-kernel-control fail-safe) and catches `! !`
from an empty variable in the argv itself, so the check works even where the plugins are
missing.

Proven, not assumed: the real `S16LE` bug was reintroduced into the real file, the suite
failed, and the tree was restored.

## 7. Known open, deliberately not fixed in this restore point

- **`power` reads `unreadable — Can't open device file: /dev/vcio_gencmd`** via the live API.
  The brownout *rate* still works (it reads `flight.txt`), so the fleet's percentage is
  trustworthy, but the live verdict is blind. Regression from the sticky-bits rework.
- **Under-voltage is real and unresolved** — `0x50000` is set. Software mitigations are
  exhausted; the buffer masks it.
- **Tailscale key expiry** and **echo cancellation** remain deferred by the owner.
- Four fleet actions remain untested because they interrupt a stream: `restart`, `reboot`,
  `jitter profile`, `lock`.

## 8. How to get back here

```bash
git checkout netbridge-MULTIRATE-VERIFIED-2026-08-13
```

Flash `MAIN-IMAGE--netbridge-os-2.0.0-bfa7336.img.xz` (Desktop, or the offline bundle at
`~/Downloads/netbridge-restore-2026-08-13/`). After flashing, `/data` is empty, so:

1. Phone → `BridgeSetup-XXXX` → Wi-Fi (there is no LAN path and no pre-staged Wi-Fi)
2. Claim it on the fleet, then **set the PIN** — a fresh `/data` has none
3. `Collect diagnostics` → `gadget-av.txt` → **`c_srate` must list all three rates.**
   If it reads `48000` alone, the image has regressed and nothing else matters.
4. Go live, switch the host format across 48k / 44.1k / 32k, and confirm the journal shows
   `capture @ <rate> Hz` for each with no `not-negotiated`
5. Listen. Every instrument on this system read clean while the operator heard jitter on the
   previous card; ears remain the deciding test.

**The tag adds evidence and a test-harness fix only — no change to any file that runs on the
bridge.** The card runs `bfa7336`; this commit's shipped-code tree is identical to it.
