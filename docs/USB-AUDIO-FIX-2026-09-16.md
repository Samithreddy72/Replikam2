# USB return-audio chipping: diagnosed and fixed on the live Pi

The user confirmed **“Chipping is gone”** after the stereo USB capture change on
2026-09-16. This was verified on device `100000005d5ade42`, kernel
`6.12.93+rpt-rpi-v8`, with the meeting laptop playing 48 kHz audio.

## Fault location and evidence

A native `arecord` recording from `hw:UAC2Gadget`, before GStreamer processing, Opus,
network transport or Mac output, already contained the audible chipping. Receiver jitter
and clock-mode adjustments could not remove it.

Mono USB capture used `c_chmask=1`, S16LE, and a 98-byte OUT endpoint maximum packet size.
The extra frame accommodates the host's packet sizing. `u_audio` spaces request buffers
by that maximum packet size; every other 98-byte stride is not 4-byte aligned. The dwc2
driver has a special bounce-buffer path for non-4-byte-aligned DMA buffers.

The mono recordings showed both large discontinuities at 48-sample USB packet boundaries
and exact non-silent sample recurrence 1536 frames earlier: 32 requests × 48 frames,
matching the configured USB request queue depth. This is strong evidence of stale USB
request-buffer data. The precise DMA/cache/bounce-buffer failure has not been instrumented
inside the kernel; do not claim a particular kernel instruction has been proven faulty.

Changing to stereo (`c_chmask=3`) makes the 48 kHz endpoint maximum packet size 196 bytes,
a multiple of four, and removes the measured corruption signature. No kernel patch or
extra receive buffering was needed. USB descriptors were re-enumerated for each change.

## Repeated native-capture comparison

Each capture is 15 s, S16LE at 48 kHz. Source playback was ongoing; these are not a
sample-aligned comparison to a known original. RMS levels were approximately -16 dBFS.

| Capture ID | USB channels | Exact non-silent recurrence at 32 ms | Largest packet-phase derivative energy / median |
| --- | ---: | ---: | ---: |
| `alsa-capture-1N7Jnh1F` | 1 | 14.394% | 60.55 |
| `alsa-capture-M57KrQya` | 2 | 0.0065%, 0.0075% | 1.06, 1.04 |
| `alsa-capture-Fln5Eisy` | 2 | 0.0066%, 0.0056% | 1.04, 1.06 |
| `alsa-capture-6zoAdQJk` (reverted) | 1 | 16.362% | 47.66 |
| `alsa-capture-Wq81t6Jd` (fixed again) | 2 | 0.0063%, 0.0070% | 1.06, 1.04 |

Metric implementation: `tools/analyze-usb-audio.py`; synthetic stale-buffer regression:
`tests/test-usb-audio-analysis.py`. These are defect signatures, not perceptual quality
scores. Periodic source signals and silence can confound exact recurrence. Near-silence
is excluded and the number of eligible samples is reported. The repeated reversal and
user listening confirmation are stronger evidence than either metric alone.

## Deployment and current settings

- Repository: `pi/scripts/uvc-raw-setup.sh` defaults USB capture to stereo. The existing
  factory image build installs this script; a new packaged image has not been built here.
- Live Pi: `/home/pi/uvc-raw-setup.sh` updated, syntax checked and byte-compared with the
  staged file; root was temporarily remounted writable and returned to read-only.
  SHA256: `e2d61a06356c7fabd5ac942b951df250058725e29fa7891453f0c31bde965e87`.
- Original live script: `/data/diagnostics/uvc-raw-setup-before-stereo.sh`.
- Original 4 GB root partition backup remains on the Mac under
  `~/.netbridge-source/card-backup-20260916/rootA.img`. The earlier
  `rootA-diagnostics.img` predates this stereo fix.
- Receive jitter latency is explicitly pinned to **0 ms** in the source app. The API now
  saves manual choices as `return_manual_jitter_ms`; fleet tuning cannot overwrite them.
  `POST /api/return-tuning` with `{"auto_jitter":true}` releases the manual choice.
- Normal receiver clock mode (`slave`), sync on, gain 1, dynamics off, FEC on, PLC off.
  Experimental clock mode `none` was not made the default.

After restoring the live session, 30 s of RTP/decoded audio were captured successfully.
At approximately 87 s receiver uptime there were 4247 pushed packets, zero lost or late
packets and no receiver failure. Some timestamp-resynchronization warnings remain: their
removal was not the fix for this audible defect. Buffer 0 is not zero end-to-end latency.

A separate 60-s Pi check saw **zero live undervoltage or throttling samples**, temperature
55.5 C. Historical `0x50000` flags remained. Kernel logs place this boot's undervoltage at
~12.9 s and normalization at ~14.9 s. Power remains a separate hardware observation;
this test does not certify the power supply under every load.

## Limits and recovery

No new kernel, NetEq implementation, clock servo, compression or gain correction was
installed to repair this fault. 44.1/32 kHz host modes and additional host operating systems
have not been hardware-validated in this session. The persistent startup file was verified;
no additional full Pi reboot was performed after the fix.

To undo the USB change, restore the saved script to `/home/pi/uvc-raw-setup.sh` while root
is temporarily writable, return root to read-only, and reboot/rebuild the gadget during a
maintenance window. Do not write channel attributes while a gadget is bound. The live test
script and capture helper restore stopped return services; they are opt-in diagnostics.

Kernel sources inspected:
- https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/usb/gadget/function/u_audio.c
- https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/usb/gadget/function/f_uac2.c
- https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/usb/dwc2/gadget.c

The inspected branch supports the alignment hypothesis; it is not a claim of an exact
source-build match to every line of the installed kernel binary.
