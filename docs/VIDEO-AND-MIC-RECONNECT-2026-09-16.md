# Video strip flicker and microphone reconnect diagnostics

The user confirmed forward audio worked, then reported a flashing bottom strip
in a Teams test call and failure to recover the selected SIMGOT USB microphone
after unplugging it. The user subsequently confirmed microphone recovery worked
and video flicker was reduced but not eliminated.

## Microphone recovery

The former watchdog exhausted five retries while a selected microphone was
absent. It also assumed a living capture process still represented the selected
input. The first reconnect trial was insufficient: the user reported hearing the
laptop microphone instead.

The user subsequently requested automatic use of the computer's default input,
including fallback when SIMGOT is unplugged. The Mac source now uses CoreAudio's
`kAudioHardwarePropertyDefaultInputDevice` (`dIn `) on each launch and every
five-second watchdog poll. It passes the resolved nonzero AudioDeviceID to
GStreamer. A default-device change restarts only voice capture; video and return
audio remain running. Reconnection follows macOS's default-input choice rather
than forcing SIMGOT. The UI labels this mode “System default microphone”.

If no system input exists at all, capture waits without exhausting the retry
budget. Ending the session still prevents recovery from restarting it. Tests cover
default-device changes, unchanged defaults, prolonged input absence, reappearance,
identity refresh, and stopped-session behavior.

## Corrected network measurement

Initial AF_PACKET counters incorrectly suggested heavy video loss. The Tailscale
interface uses UDP receive aggregation: one captured buffer can contain multiple
RTP datagrams. A video buffer of 2,228 bytes contained two 1,100-byte RTP packets
plus IP/UDP headers. Counting only the first RTP header created false sequence gaps.

Using PACKET_VNET_HDR and its UDP segmentation size to split captured buffers
correctly produced **2,740 video and 750 audio packets in 15 seconds, zero sequence
gaps, and zero capture socket drops**. Previous raw-sniffer loss estimates are
invalid, including those originally reported in the microphone investigation.
This does not invalidate independent AVFoundation sample-timestamp gap tests.

The mesh helper now logs input RTP sequence gaps and maximum write time every ten
seconds and requests 1 MiB local receive capacity for encoder bursts. Its counters
also showed no input gaps. The helper was rebuilt from the repository with its
pinned Tailscale dependency, tested live, and installed in the source runtime.
The original binary is preserved under `~/.netbridge-source/tools/`.

## USB video mitigation

- Decoder/loopback format is 640×360 YUYV, stride 1,280 bytes, full frame 460,800
  bytes. The gadget pump reported that full frame size.
- A ten-second H.264 capture before USB was reconstructed with aggregation-aware
  parsing. Sampled decoded frames showed no bottom-strip corruption. The capture
  joins an existing stream; initial missing-SPS/PPS messages precede the next IDR.
- While video was active, the kernel reported `uvc: VS request completed with
  status -61` (ENODATA), consistent with failed isochronous transfers. This alone
  does not prove which visible artifact a host application displays.
- The DWC2 interrupt was being serviced on CPU 0 despite its allowed mask 0–3.
  Moving it to CPU 2 reduced logged errors from 78 in a preceding 20-second window
  to 5 in the next window, then 1 in another 20-second window. The pump remained
  active, with approximately 42–50 dequeues per two-second report.
- Temperature was 59.4°C; throttling value remained 0x50000 (historical flags,
  no current undervoltage/throttling bits).

`pi/scripts/bridge-uvcd.sh` now resolves the Pi 4 DWC2 IRQ number dynamically and
assigns it to online CPU 2 before launching the gadget daemon. Other hardware or
an affinity-write failure keeps system behavior and does not block camera startup.
No frame size, bitrate, frame rate, jitter buffer, or CPU governor was changed.

The startup wrapper was installed persistently at `/usr/local/bin/bridge-uvcd.sh`,
including support for CPU 2 having no optional sysfs `online` file.
The live IRQ change was already active, so installation did not require another
video restart. The root filesystem was returned to read-only.

Rollback: restore `/data/diagnostics/bridge-uvcd-before-irq.sh` using the standard
root remount procedure and restore the affinity recorded in
`/data/diagnostics/usb-irq-affinity-before.txt` (0–3 for this trial). No card reflash
is needed. The error reduction is measured; complete visual resolution is not yet
confirmed.
