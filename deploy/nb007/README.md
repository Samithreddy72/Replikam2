# NB 007 only: clean camera picture on a Mac room, as a signed update (no new image)

## Why

NB 007 (2.2.2-2c29020) sends a YUY2 **424×240** camera frame, so each row is 848 bytes. macOS's UVC driver
assumes every row arrives padded to a multiple of 64 bytes and reads each one as 896, which shears the
picture into slanted stripes. Windows and Linux read packed rows and are unaffected.

This update makes NB 007 advertise **480×212**. Its rows are 960 bytes, which is 15 × 64, so macOS reads
them correctly. The frame is still **203,520 bytes**, so NB 007's compiled camera pump and status page
carry it unchanged. The 16:9 picture sits in the middle with black side bars.

| Proof | Result |
|---|---|
| Apple's own `UVCFrameProcessor` copy routine, run on a Mac | 424×240: 239/240 rows corrupted. **480×212: 0/212**, on every path |
| NB 007's unchanged 2.2.2 pump code, compiled and run | `set_format` -22 is ignored, the 203,520-byte buffer is filled, and the payload is byte-identical |
| Linux 6.12.93+rpt gadget sources (NB 007's kernel) | 480×212 gives bytesperline 960 and sizeimage 203,520; streaming is a byte copy |
| This exact deploy file, run end to end with GStreamer | About 30 fps of 203,520-byte frames. Picture at columns 52–425, black bars, log line `Video frame 480x212` |
| Stock 424×240 path | The pipeline string is byte-identical to 2c29020 |
| `tools/run-tests.sh --suite common` | 1694 passed, the same 3 failures and 6 no-results as before, with no regressions |

## Files

Send only these two, to NB 007 only:

| File | sha256 | Catalog kind / apply mode |
|---|---|---|
| `deploy/nb007/bridge-feeder-net.sh` | `59ec2d9d06f88618f7e2685aed17867176fa1bc385b1e6d5222311c932eb188f` | loader / video |
| `deploy/nb007/uvc-raw-setup.sh` | `4624e294b0fc4fa8eef521cbd39d23bf2505ae236e131e12ac77ce7a6f04c2c1` | bind / boot |

- **`bridge-feeder-net.sh`** is the stock loader plus the receiver (`pi/scripts/bridge-video-receiver.py`
  on this branch). The receiver follows the advertised frame. With 424×240 it behaves exactly as stock.
  The trailing `bridge-video-receiver.py` argument keeps the bridge's live-session checks working.
- **`uvc-raw-setup.sh`** is the stock descriptor script. Only `create_frame … 480 212` and its comment differ.

> ⚠️ **NB 007 only.** Never deploy or install these names on another bridge.
>
> - Publishing replaces the fleet's single catalog copy of each name. When NB 007 is done, republish the
>   stock files (see "Afterwards").
> - An OS update on NB 007 later disables both files. Redeploy them afterwards, or the stripes return.
> - The two files belong together. If either is parked or reverted, revert the other and reboot.

## Before starting

Run everything from `~/netbridge/wt/nb007-mac-update`. `tools/nb` isn't on PATH, and the main checkout
lacks it.

- **P1.** Nobody presenting on NB 007 for about 10 minutes. Only the reboot interrupts the room, for 1–2
  minutes.
- **P2.** Check the fingerprints match the table above:
  ```bash
  shasum -a 256 deploy/nb007/*.sh
  ```
- **P3.** Check NB 007's state:
  ```bash
  tools/nb show NB-007
  ```
  It must be online on 2.2.2, with no presenter live, overrides pending `[]` and parked `[]`. If any
  deploy-script, revert-script or reboot is pending, waiting or sent, cancel it first:
  `tools/nb cancel NB-007 <id>`.
- **P4.** Check what's running:
  ```bash
  tools/nb running NB-007
  ```
  It must show `bridge-feeder-net.sh baked-in sha256=beba80a36c32` and no `uvc-raw-setup.sh` line.

**Which path:** NB 007 refuses deploys and reboots while the room Mac is plugged into it.

- **Path A (preferred):** NB 007 has power that isn't the Mac's cable. Unplug the Mac, then use plain
  `tools/nb deploy` and `tools/nb reboot`.
- **Path B:** the Mac's cable is NB 007's only power. Use the fleet's audited live override (`force_live`),
  **only with Samith's explicit OK.** Define this helper once:

```bash
nblive() { python3 -B -c '
import importlib.machinery as m, importlib.util as u, sys
l = m.SourceFileLoader("nbtool", "tools/nb"); nb = u.module_from_spec(u.spec_from_loader("nbtool", l)); l.exec_module(nb)
t, n = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
a = {"name": n, "source": nb.FLEET + "/payloads"} if t == "deploy-script" else {"name": n} if t == "revert-script" else {}
d = nb.pick("NB-007")[0]
nb.confirm("LIVE OVERRIDE (meeting guard bypassed) %s %s" % (t, n or ""), [d])
sys.exit(nb.show_result(nb.run(d, t, a, extra={"when": "now", "force_live": True})))
' "$@"; }
```

It prints a warning that a meeting laptop is attached, then asks to continue. **Abort if it says NB 007
is LIVE.**

## Send it

Do the steps in this order, and start each one only after the previous one shows ✔ done.

1. **Publish the feeder:**
   ```bash
   bash tools/publish-script.sh deploy/nb007/bridge-feeder-net.sh
   ```
   Expect `✅ signed bridge-feeder-net.sh (loader)` and `✅ published to the fleet`. Ignore its "Deploy it:"
   hint.
2. **Deploy the feeder:**
   - Path A: `tools/nb deploy NB-007 deploy/nb007/bridge-feeder-net.sh`, then wait until it's done.
   - Path B: `nblive deploy-script bridge-feeder-net.sh`, then answer y.

   Expect `signature OK`, `loader check OK`, `installed /data/overrides/bridge-feeder-net.sh`, then
   `bridge-deploy: done`. On path B it then says *pending until the laptop is unplugged*, and that's
   expected. If it says `SIGNATURE VERIFY FAILED` or `identity or revision refused`, stop: nothing was
   installed.
3. **Publish and deploy the descriptor:**
   ```bash
   bash tools/publish-script.sh deploy/nb007/uvc-raw-setup.sh
   ```
   Then send it:
   - Path A: `tools/nb deploy NB-007 deploy/nb007/uvc-raw-setup.sh`
   - Path B: `nblive deploy-script uvc-raw-setup.sh`

   Expect `bind check OK` and `installed — takes effect at the next reboot`. Until step 4, don't run
   reset-clock or restart-all.
4. **Reboot NB 007** once both deploys are done and nobody is presenting:
   - Path A: `tools/nb reboot NB-007`
   - Path B: `nblive reboot`

   The room loses its camera, mic and speaker for 1–2 minutes.

## Check it (about 4 minutes after the reboot)

1. Run `tools/nb show NB-007`. Overrides must show `boot_attempts 0`, active
   `["bridge-feeder-net.sh", "uvc-raw-setup.sh"]`, pending `[]` and parked `[]`.
2. Read the new frame size:
   ```bash
   tools/nb read NB-007 /sys/kernel/config/usb_gadget/g1/functions/uvc.0/streaming/uncompressed/u/212p/wWidth
   ```
   It must give **480**, and the same path ending `wHeight` must give **212**.
3. Run `tools/nb running NB-007`. Both lines must say `override`, with nothing PENDING and nothing
   quarantined.
4. Go live from Studio. The room Mac's Photo Booth or meeting app must show the 16:9 picture with black
   side bars and **no slanted stripes**. In `tools/nb logs NB-007 bridge-uvcd -n 200`, the line
   `Setting format to 0x56595559 480x212` must appear, and the feeder log must show
   `Video frame 480x212 (from the USB descriptor)`.
5. Compare with `~/Downloads/NB007-video-before-after.md`. Run `~/Downloads/NB007-video-check.sh 212p`
   while live. Expect about 30 fps, 0 corrupt frames and low USB misses.

A black camera with no presenter is normal idle and proves nothing. Judge only with a presenter live.

## Roll back

Undo in reverse order: descriptor first, then the feeder, then reboot. Use the same path as for sending.

1. Revert the descriptor:
   - Path A: `tools/nb revert NB-007 uvc-raw-setup.sh`
   - Path B: `nblive revert-script uvc-raw-setup.sh`
2. Revert the feeder:
   - Path A: `tools/nb revert NB-007 bridge-feeder-net.sh`
   - Path B: `nblive revert-script bridge-feeder-net.sh`
3. Reboot NB 007:
   - Path A: `tools/nb reboot NB-007`
   - Path B: `nblive reboot`
4. Check: `u/240p/wWidth` reads 424, and `tools/nb running NB-007` shows `bridge-feeder-net.sh baked-in`
   with no uvc line.

## Afterwards (keeps every other bridge stock)

Put the stock files back as the fleet's catalog copies. This installs nothing on any bridge, NB 007
included.

```bash
bash tools/publish-script.sh pi/scripts/uvc-raw-setup.sh
```

```bash
bash tools/publish-script.sh pi/scripts/bridge-feeder-net.sh
```
