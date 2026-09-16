# UVC DWC2 missed-transfer trial

`uvc-dwc2-missed-transfer.patch` adds `-ENODATA` to the UVC driver's existing
incomplete-transfer handling, with a diagnostic log, rather than cancelling the
video queue through the generic completion-error path. DWC2 returns this status
when an isochronous target frame has already elapsed. The existing `-EXDEV` path
marks the frame incomplete and sets the error bit in subsequent UVC headers.

This is a **staged experimental patch, not a confirmed flicker fix**.

Matched target:

- Kernel: `6.12.93+rpt-rpi-v8`, Debian package `1:6.12.93-1+rpt1`.
- Raspberry Pi Linux commit: `d8ab4e908235da7727f22dd36ad5af224671677d`, recorded
  in the installed kernel package's Debian changelog.
- Unmodified rebuild source fingerprint: `DE07986681E4A6F2055E84B`, identical
  to the installed `usb_f_uvc` module.
- Patched rebuild source fingerprint: `F488A0D08CA1D279FFAD2A6`.
- Both built against the installed kernel headers with matching module version
  magic and symbol-version checks. No forced module loading is used.

Build files and module are staged on the diagnostic Pi under
`/data/diagnostics/netbridge-uvc-module/`; the unmodified rebuild is preserved as
`/data/diagnostics/usb_f_uvc-unmodified.ko`. The normal module under `/lib/modules`
has not been replaced, so reboot loads the original driver.

Live activation could not be verified: the Pi became unreachable around the test
attempt, and the SSH call produced no remote output before cancellation. It is
unknown whether the remote script began or loaded the module. Do not claim that
the patch ran, fixed flicker, or caused the loss of connectivity without reading
the recovered Pi's state and logs. On recovery, check
`/sys/module/usb_f_uvc/srcversion` and kernel logs before any further trial.

The earlier performance-governor trial showed no meaningful reduction in errors;
the governor was restored to `ondemand`. No larger USB-transfer descriptor was
applied: the proposed 3,072-byte setting exceeds the available 2,048-byte TX FIFO.

Recovery confirmed the original driver fingerprint was active after reboot.
The IRQ mitigation is active, but the user still reports flicker and -61 errors
remain. The next runtime trial uses `pi/diagnostics/test-uvc-module.sh`, which
requires physical USB detachment, pauses the periodic recovery watchdog during
the swap, and logs its stages under /data/diagnostics. Physical detachment is
required because this project's bridge-uvcd service documents DWC2 hangs during
gadget churn with a host attached. The normal module remains unchanged.

## Confirmed detached activation

On 2026-09-16 at approximately 17:49 UTC, the detached trial successfully loaded
fingerprint `F488A0D08CA1D279FFAD2A6`. Video, forward audio, return audio, and
the recovery timer were active; stereo capture mask remained 3 and USB IRQ
affinity remained CPU 2. Root remained read-only. Visual Teams testing is pending.
The first detached attempt rolled back because removing the original module
also unloaded dependencies; the script now explicitly reloads the dependencies
listed by modinfo before insmod. No forced module loading or installed-module
replacement was used. Reboot still restores the original module.

Brief undervoltage events were logged after USB detachment (17:47–17:48 UTC);
the subsequent throttle reading was 0x50000 (historical, not active flags).

## Trial rolled back

The user reported both USB audio and video inputs missing after the trial.
The Pi reported UDC state `not attached`, so host enumeration and visual results
were not verified. Restored the unmodified module; confirmed fingerprint
`DE07986681E4A6F2055E84B`, all four media services active, stereo capture mask 3.
UDC still reported `not attached` after restoration. Do not deploy the patch as
a confirmed fix; USB reconnection/enumeration must be restored first.

## Trial reapplied after user clarification

The user clarified that the missing USB inputs were due to a different issue
and requested another experimental-driver trial. After verifying physical USB
detachment, reapplied the patch and confirmed fingerprint
`F488A0D08CA1D279FFAD2A6`. All four media services and the recovery timer were
active; stereo capture mask remained 3 and USB IRQ affinity remained CPU 2.
The patch is currently active for testing only; host enumeration and flicker
feedback after reconnection remain pending. Reboot restores the original driver.

The user subsequently reported unchanged flicker. Confirmed the patched module
was loaded, UDC configured, and its -61 missed-transfer path was executing.
The patch has not demonstrated a visual benefit and must not be promoted to
a permanent fix. A two-minute Pi-local moving SMPTE pattern at 640x360 YUY2
20 fps was started using a runtime-only feeder override, with a systemd timer
to remove the override and restore the network feeder automatically. This
isolates the source camera/encoder/network from the loopback/USB/host path.

The user confirmed the Pi-local test bars also flicker. This excludes the Mac
camera, encoder, and network as necessary triggers for this reproduction; it
does not isolate USB from the Pi loopback/gadget or Teams renderer. The user
also confirmed a hub/dock in the USB path. Next A/B test: direct Pi-to-meeting-
laptop USB with the same video settings, before further driver changes.
