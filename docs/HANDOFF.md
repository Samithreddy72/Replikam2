# NetBridge — handoff

Written 25 August 2026. Everything a fresh session needs to continue without repeating work
that has already been done, or repeating mistakes that have already been made.

---

## 1. What the product is

A Raspberry Pi 4 presenting itself as a **USB webcam, microphone and speaker** to a meeting
laptop in a room. A remote presenter's Mac sends video and voice to the Pi over a mesh; the Pi
plays them into the meeting as if it were a physical camera. The room's audio comes back the
other way so the presenter can hear it.

```
presenter Mac ──mesh──> Pi bridge ──USB──> meeting laptop (Teams/Zoom/Meet)
      ^                                             |
      └──────────── return audio ───────────────────┘
```

Two pieces of software, both of which must be right:

| | |
|---|---|
| **the image** | what runs on the Pi. Built by CI, flashed to an SD card |
| **the app** | `NetBridgeSource`, a single binary the presenter runs on their Mac |

Plus a **control plane** (`fleet.scine.online`) that the operator uses to see and command
bridges.

---

## 2. Current state

```
bridge      netbridge-os-2.0.0-1db20ec   online, enrolled, USB attached
app         1.1.9                        macOS, released on GitHub
control     fleet.scine.online           deployed 24 Aug, accepts all 26 commands
repo        github.com/Samithreddy72/Replikam2  (PRIVATE)
tests       258 passing
```

**Audio is clean as of this writing.** The jitter that dominated the last week was last heard
on 24 August and has not returned since the current image and app went on.

### Where the images live

`~/Desktop/NetBridge-Image/` — three slots, newest first, each with a signed manifest:

```
1--FLASH-THIS--…1db20ec--settling-signal-hostname-version-audited-86of86
2--ROLLBACK--…3aebd08--PROVEN-ON-HARDWARE-clean-audio
3--ROLLBACK-OLDER--…bfa7336--PROVEN-ON-HARDWARE-running-now
Archive/  everything older
```

Filenames carry the **build time in local wall-clock**, so Archive sorts oldest-to-newest by
itself. `ROLLBACK.txt` in that folder is the operator-facing guide and is kept current.

Rotation is done by `tools/stage-image.sh <commit> "<label>"`, which downloads, verifies the
signature and checksum, unpacks, **audits, and only then touches the folder**. A failed audit
leaves slot 1 alone.

---

## 3. The tools, and what each is for

| tool | what it answers |
|---|---|
| `tools/image-audit.sh <img>` | 86 checks on a built image, before flashing |
| `tools/app-audit.sh [dir]` | 30 checks on the presenter app bundle |
| `tools/session-end-check.sh` | did ending a session actually end it, on both ends |
| `tools/nb-read.sh <path>` | read any file on a live bridge, read-only |
| `tools/nb-snapshot.py` | capture all three devices at once; `--compare a b` diffs them |
| `tools/stage-image.sh` | download → verify → audit → rotate the flash folder |
| `tools/fix-camera-macos.sh` | clear a wedged macOS camera (needs the user's password) |

### `read-file` — how the fleet reaches a bridge

Built after the operator asked for "full access to the Pi" and rejected Tailscale SSH. The
product walkthrough is explicit that SSH is gone and that commands are re-validated on the
device against an allow-list, so a run-anything button would have contradicted the design.
**Read-only was the answer, because every question that has actually mattered was a read.**

```
bash tools/nb-read.sh /proc/asound/UAC2Gadget/pcm0c/sub0/status
bash tools/nb-read.sh --list /data
```
Panel: Actions → *Look at it* → "Read a file on the bridge…"

Readable roots: `/proc /sys /etc/bridge /etc/default /etc/netbridge /data /var/log
/usr/local/bin /home/pi /run`. The **device** enforces this — refuses paths outside the roots
and symlinks that escape them (checked on the resolved path), refuses credential files,
redacts token-shaped values inside permitted files, caps at 64 KB.

It answered five real questions on 24–25 August that would each have cost a rebuild.

---

## 4. Things that were believed and turned out to be false

**Read this section before proposing anything.** Every item cost real time.

### The "0.710 ms/s of lost audio" figure was an artifact
`capture-gap-probe.py` summed only the shortfalls of a noisy signal, which returns a large
number from a stream losing nothing. Corrected: **−96 ppm ± 183, within noise of zero.**
Nothing is lost at the capture. Everything built on that number is withdrawn, including
"`req_number` 8→32 made no difference" — that was noise compared against noise, so the
question is **open**, not answered.

### The jitter was never the image
The bridge was rolled back through **two image generations** and the jitter followed. Later the
app was rolled back too. Neither fixed it. Both were eliminated by experiment, not argument.

### The app version correlation was coincidence
Four clean snapshots were on app 1.1.5 and two jittery ones on 1.1.6, which looked decisive.
It wasn't: 1.1.6's only functional change fires when the fleet has published tuning, which was
null on both bridges. Then the jitter returned on 1.1.7.

### Periodic stalls in the flight recorder are the recorder, not the Pi
A pair of missed ticks every ~10 s looked like it tracked the Mac's poll interval. Tripling
the polling changed nothing. It is the recorder mis-timing its own one-second loop.

### Two checks that could not fail
`lsof | grep AppleH1xCamIn` **passed while ffmpeg was actively capturing**, and a shell range
like `/def foo/,/^    def [a-z]/` ends on the line it starts, so every check inside it silently
passed. A check that cannot fail is worse than no check, because it reads as reassurance.

---

## 5. What is still open

### The jitter root cause — unknown
Excluded with measurements, not reasoning:

| theory | evidence against |
|---|---|
| capture loss | net rate error within noise of zero |
| clock drift | ring never fills past one period (`avail_max` 960) |
| dwc2 SOF storm | 5,041 interrupts/s measured; that fault produces 250,000+ |
| the image | rolled back two generations, jitter followed |
| the app | rolled back, jitter followed |
| the Mac's socket | 0 drops, 0 bad checksums over 30 s |
| polling load | tripled it, nothing changed |

**The strongest surviving correlation is power.** Four clean-audio references all read
`throttled=0x0`. Every jittery measurement read `0x50000` with live undervoltage events in the
kernel log. Wi-Fi degraded across the same measurements (−38 → −48 → −58 dBm), and a weak radio
transmits harder and retries more, which is one of the largest bursty current draws on a Pi 4 —
so the loss and the brownouts may be **the same event**.

> ⛔ **The 5V/3A power-supply change is permanently closed by the operator. Do not raise it.**
> The measurement may be reported; the remedy may not be proposed. The lever that remains is
> the **radio** — placement, band, channel.

### `req_number` 8 vs 32 — genuinely unanswered
The right metric is **exact-zero runs in captured decoded audio**, not rate error. Rate error
measures the clock; `req_number` is tolerance to driver lateness. Both images now expose
`gadget-tune`, so the A/B is finally possible.

### Windows app build — blocked upstream
Chocolatey's GStreamer package fails its own checksum. **Do not use `--ignore-checksums`**:
GStreamer is the only return-audio player on Windows, so that build would look successful and
have no room audio. Releases now name the missing platform explicitly.

### Hostname rename — cosmetic, deprioritised by the operator
Every card is still `raspberrypi`. firstboot runs and reaches the code (verified by reading
the script off the card and confirming `/data/.expanded`), but the kernel hostname does not
change. Leading theory: the root is a read-only overlay and `hostnamectl` returns 0 while doing
nothing, so the working fallback never fires. **Only matters with two bridges on one LAN**
(mDNS collision). Not worth fixing until then, and worth testing on hardware when it is.

### Untested on hardware
The pitch controller (inert unless `c_sync=async`), the gadget tuning file, and the `lock`
action.

---

## 6. Rules the operator has set

These are standing constraints, not preferences.

- **Never suggest the 5V/3A power-supply change.** Closed permanently.
- **No Tailscale SSH.** Remote access is `read-file` only.
- **No LAN-direct provisioning.** Not in the product walkthrough.
- **No pre-staged Wi-Fi** — the setup portal is the only way a bridge joins a network. The
  setup-AP password is the fixed `bridge2626` for every bridge.
- **Never write test rows into the live commands table.** The agent executes them. This was
  violated once on 24 August while probing which commands the backend accepted; it queued a
  real `reboot` that could not be cancelled (there is no cancel endpoint) and rebooted the
  bridge. Probe against the repo, never the live queue.
- **Ship only on explicit command.** Build and hold; do not deploy or flash unasked.
- **Card surgery over reflashing** where a change can be made on the card directly.
- Commit as `Samithreddy72 / samithreddy72@gmail.com`, no `Co-Authored-By` trailer.
- Never suggest wrapping up, resting, or that it is late.
- Bridges are named in the fleet panel after **whoever currently holds the device** (e.g.
  "Scine Test"), not after a location — they circulate between people.

---

## 7. Facts that cost hours to rediscover

- The UAC2/UVC descriptor is **`/home/pi/uvc-raw-setup.sh`**, not `/usr/local/bin`.
- `/etc/bridge` is **bind-mounted from `/data/etc-bridge`**, so anything the image build
  writes into the rootfs copy is invisible at runtime. The version bug was "fixed" once
  against the shadowed path and changed nothing.
- The fleet config is **seeded onto `/data` at build time**; the rootfs
  `/etc/default/bridge-agent` is a deliberate **zero-byte bind target**. An empty file there
  is correct, not a broken provision.
- **`bridge-firstboot.sh` exits early on every card** at "no provision conf found", because
  these images do not ship that file. Anything that must always run has to sit above that line.
- `bridge-agent` is started by **`bridge-agent.timer`** and correctly has no `WantedBy`.
- Real unit names are `bridge-gadget` and `bridge-supervisor` — not `netbridge-gadget` or
  `bridge-media`.
- `bridge-feeder`, `bridge-testpattern`, `bridge-soak` **must stay disabled**;
  `bridge-testpattern` declares `Conflicts=bridge-feeder-net`.
- `bridge-crackle-sentry` and `bridge-pitch` ship **disabled** as of 24 August. Neither had
  ever been shown to help, and `bridge-pitch` does nothing unless `c_sync=async`.
- `mesh-key` is a **control-plane endpoint**, never a device command.
- **Flashing rewrites `/data` too**, so `/data/.expanded` is wiped and the filesystem
  expansion re-runs on every flash — `resize2fs` plus `ssh-keygen -A` against a real-time
  media pipeline. That is what causes loud periodic bursts if you go live immediately. The
  image now reports `settling` and says so in `/api/checks`.
- The bridge encodes return audio with **`inband-fec=true packet-loss-percentage=20`** — a
  fifth of its bitrate is redundancy. The Mac must have `use-inband-fec` on to use it. FEC and
  PLC are now separate switches (FEC on, PLC off) because PLC *invents* audio while FEC
  *rebuilds* from data actually sent.
- Command output from the fleet contains **raw newlines inside JSON strings**; parse with
  `json.loads(..., strict=False)`.
- macOS attributes camera access to the **responsible parent process**. Launching the app from
  a shell gives an encoder that runs happily and receives **zero frames**, with no green light.
  The app must be launched by the user (`Launch NetBridge.command`).
- A **SIGKILLed ffmpeg does not release the camera**, leaving the macOS daemons handing out a
  device that delivers nothing. Ask it to quit (`q` on stdin) before SIGTERM, SIGKILL last.

---

## 8. How to check things

```bash
# is the bridge healthy?
curl -s http://<bridge-ip>:8080/api/status | python3 -m json.tool

# what does the app see?
curl -s http://127.0.0.1:8765/api/state
curl -s "http://127.0.0.1:8765/api/checks?host=<bridge-ip>"

# take a reference while audio is GOOD, and compare when it is bad
python3 tools/nb-snapshot.py --label good --deep
python3 tools/nb-snapshot.py --compare good bad

# did the session really end?
bash tools/session-end-check.sh <bridge-ip>

# read anything on the bridge
bash tools/nb-read.sh /home/pi/flight.txt
```

Healthy reference snapshots are banked in `~/Downloads/netbridge-snapshots/`. **Their spread
is itself the finding** — rate error wanders ±500 ppm and path spread varies 3× while the
audio sounds perfect, so anything inside those bands is normal.

---

## 9. Working habits that were learned the hard way

- **Take a healthy reference before diagnosing.** Every wrong turn came from judging one
  reading on whether it "looked wrong", with nothing to compare it against.
- **A one-sided sum of a noisy signal is not a measurement.** Report a mean with a standard
  error.
- **Run the negative control.** An audit that has never been shown to fail proves nothing —
  run it against something known to be broken first.
- **Never change the gadget on a running bridge.** Two outages, both needing a physical power
  cycle, both from writes that could have been reads.
- **Say which END is at fault before sending anyone to look.** Sending an operator to the far
  end of a healthy link is the single most expensive mistake this project keeps making.
- **Verify on the device, not in the source.** The version bug, the firstboot early exit and
  the hostname failure were all invisible in the repo and obvious from one read of the card.
