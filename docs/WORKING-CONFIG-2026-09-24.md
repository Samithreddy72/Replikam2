# NetBridge — verified working configuration (2026-09-24)

**Status:** verified at 20:34 IST and re-checked at 20:49 IST. The presenter confirmed smooth video, no
freezes and no latency issues. Video, voice and return audio were all live, with 0 media-service restarts
and 0 lost packets from the Mac to the Pi.

**Sources.** Every value below was read from one of these:
- the **running** processes on the Mac and the Pi;
- the live USB gadget (configfs);
- the Pi's own files.

Each value was then cross-checked against the code on branch `Everything-good-(sound+audio)`. Nothing was changed
to take these readings.

A Word copy is next to this file: `docs/NetBridge-Working-Configuration-2026-09-24.docx`.

---

## 1. Components and versions

| Part | Version / setting |
|---|---|
| Pi image | `2.1.0-52a161b`: GitHub draft release `v2.1.0-52a161b`, image sha256 `0f316e5067ed3cc9596259053bd24445087232f0835fb84ecca7d5b6b5a60921`, kernel `6.12.93+rpt-rpi-v8` (pinned as `kernel612.img`). It was audited before flashing: `tools/image-audit.sh` passed 107/107 and the file-by-file verifier passed 54/54. All 930 installed packages are identical to image 8434651 |
| Pi camera service | Signed override `97219e9c` at `/data/overrides/bridge-uvcd.sh`. It is the image's camera script plus a USB-miss counter, with the USB interrupt (IRQ 34) on CPU 2. **Since commit 6f8a60a this is also the built-in `pi/scripts/bridge-uvcd.sh`**, so an image built from this branch starts this way without the override |
| Other Pi media scripts | Built in, with no overrides: feeder-net `23cb9bca`, feeder-audio `c4181446`, return-audio `282db308` |
| Mac app | NetBridge **1.4.3**, the default app in `~/Desktop/NetBridge`. Older builds are in its `Older versions` folder |
| Mesh | Tailscale, on the **direct** path over the local Wi-Fi |
| Media profile | **WAN** (`NET_VIDEO_LATENCY=300`, `NET_AUDIO_LATENCY=300`), seeded in `/data/config/bridge-net` and bind-mounted onto `/etc/default/bridge-net`. The video feeder caps video at 100 ms |
| Code | Branch `Everything-good-(sound+audio)` (renamed on 2026-09-24 from `opt/pi-2026-09-22`, the name used in the image audit reports; pushed; **not merged into `main`**) |

---

## 2. Video: Mac camera → Pi → meeting laptop (USB camera)

| Stage | Setting |
|---|---|
| Mac capture | MacBook Air camera via AVFoundation (`-f avfoundation -framerate 30 -video_size 1280x720 -pixel_format uyvy422`). Asking the camera for 320×180 or 640×360 fails outright, so the app captures large and scales down |
| Mac scale | `-vf scale=424:240,format=nv12` |
| Mac frame rate | **30 fps constant** (`-fps_mode cfr -r 30`), the camera's native rate. 20 fps was dropped because keeping 2 of every 3 frames spaced them 33/67 ms apart, which showed as judder |
| Mac encoder | `h264_videotoolbox -realtime 1 -profile:v baseline`, `-b:v 600k`, `-g 30` (one keyframe per second), `-bsf:v dump_extra=freq=keyframe` (SPS/PPS repeated on every keyframe). Baseline uses CAVLC, which measured 41% less Pi decode CPU than High/CABAC with no visible difference (SSIM 0.966 vs 0.967) |
| Mac transport | RTP payload 96, `pkt_size=1100` → `127.0.0.1:5000` → mesh helper `netbridge-mesh` (1 MiB socket buffers, logs packets/gaps every 10 s) → Pi port 5000 |
| Pi jitter buffer | `rtpjitterbuffer latency=100`. The WAN profile says 300, but `bridge-feeder-net.sh` caps video at 100 ms; a non-numeric value falls back to 100 |
| Pi decode | `rtph264depay ! h264parse ! avdec_h264` (**software**). The hardware decoder is deliberately unused (`gpu_mem=16`): it measured 74% of a core against 51%, because its output conversion cost more than the decode it saved |
| Pi convert | `videoconvert ! videoscale ! videorate ! video/x-raw,format=YUY2,width=424,height=240,framerate=30/1`. No actual scaling happens |
| Pi hand-off | `v4l2sink device=/dev/video40 sync=false` → v4l2loopback (`video_nr=40 card_label=BridgeCam exclusive_caps=1 max_buffers=16`, `YUYV:424x240@30`) → `uvc-gadget` pump. The pump re-sends the latest frame when no new one is ready |
| USB camera (UVC) | Uncompressed **YUY2 424×240 @ 30 fps** (`dwFrameInterval 333333`), frame buffer 203,520 bytes (`dwMaxVideoFrameBufferSize`) |
| USB transport | Isochronous, `streaming_maxpacket 1024`: **one 1,024-byte packet per 125 µs microframe**, with no high-bandwidth mode, on USB 2.0 high-speed. The stream needs 6.1 MB/s, 75% of the single-packet ceiling of 8.2 MB/s. 640×360@20 needed 9.2 MB/s, which forced 2048 (two packets per slot); that is the mode where the Pi's dwc2 controller missed slots and every meeting app showed freezes |
| USB interrupt | DWC2 IRQ 34 pinned to **CPU 2** (`USB_IRQ_CPUS="2"`). WaysToGo measured 78 `-61` misses per 20 s on CPU 0 and 1–5 on CPU 2 (2026-09-16). Setting it to `"0-3"` restores the kernel default for A/B tests |
| Idle picture | A black 424×240 frame (`/etc/bridge/idle-frame.raw`, 203,520 bytes), sent by the pump after 5 s without a fresh frame (`IDLE_FALLBACK_S 5`) |
| Camera logs | The `unable to dequeue buffer … (11)` EAGAIN flood is filtered out. Pump telemetry every 2 s: `pump: ok= again= err= gray= idle=` |
| USB-miss counter | Written to `/run/netbridge-usb-video.txt`. Every 10 s it records `enodata` (-61), `exdev` (-18 / missed xfer), `other`, and the per-CPU USB interrupt load; it keeps the last 360 lines. Read it with the fleet `read-file` command (section 9) |

---

## 3. Audio: voice, Mac microphone → Pi → meeting laptop's microphone (unchanged)

| Stage | Setting |
|---|---|
| Mac capture | "System default microphone": `osxaudiosrc device=0 buffer-time=40000 latency-time=10000` (backend `gstreamer-coreaudio`). Mic boost 0 dB, not muted |
| Mac format | 48 kHz stereo F32LE, then S16LE |
| Mac encoder | `opusenc bitrate=64000 audio-type=voice frame-size=20 inband-fec=true packet-loss-percentage=5` |
| Mac transport | `rtpopuspay pt=97` → `127.0.0.1:5002` → mesh → Pi port 5002 |
| Pi jitter buffer | `rtpjitterbuffer latency=300 do-lost=true` (the script default is 120; the WAN profile sets 300) |
| Pi decode | `opusdec plc=true use-inband-fec=true` |
| Pi processing | 48 kHz stereo S16LE → `webrtcdsp`: **gain-control on** (`target-level-dbfs=6`, i.e. −6 dBFS; `compression-gain-db=6`; `limiter=true`), `high-pass-filter=true`, `noise-suppression=false`, `echo-cancel=false` |
| Pi output | `queue max-size-time=400 ms leaky=downstream` → `alsasink device=plughw:UAC2Gadget sync=false buffer-time=200000 latency-time=40000`. The echo-cancel reference branch is built only when `RETURN_AEC=1`, and that is off |
| USB microphone (UAC2) | Stereo (`p_chmask 3`), 16-bit (`p_ssize 2`), offering `48000,44100,32000`. **The laptop opened 48 kHz stereo** (period 1024, buffer 9216 frames) |

## 4. Audio: return, meeting laptop's speaker → Pi → Mac (unchanged)

| Stage | Setting |
|---|---|
| USB speaker (UAC2) | Stereo (`c_chmask 3`), 16-bit (`c_ssize 2`), `c_sync adaptive`, `req_number 32` (tunable in `/data/gadget-tuning.conf`), offering `48000,44100,32000`. **The laptop opened 48 kHz stereo** (period 960, buffer 9600 frames). Rate follow is on, with no mismatch |
| Pi capture | `alsasrc device=hw:UAC2Gadget buffer-time=200000 latency-time=20000` at the laptop's rate (allowed rates 32000, 44100 and 48000), then `queue max-size-time=300 ms leaky=downstream` |
| Pi convert | `audioresample quality=10` → 48 kHz stereo S16LE |
| Pi encoder | `opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20` → `rtpopuspay pt=97` → Mac port 5004. The app sets the destination at go-live |
| Mac jitter buffer | `rtpjitterbuffer latency=250 do-lost=true` |
| Mac playback | `opusdec use-inband-fec=true` (PLC off, concealment off) → `audioresample quality=10` → `volume 1.0` → `queue max-size-time=400 ms` → `osxaudiosink sync=true buffer-time=200000 latency-time=20000` |

---

## 5. Pi system configuration (image 52a161b)

| Area | Setting |
|---|---|
| Boot, `config.txt` `[all]` | `dtoverlay=dwc2,dr_mode=peripheral`, `dtoverlay=disable-bt`, `arm_freq=900`, `arm_boost=0`, `gpu_mem=16`, `dtparam=audio=off`, activity and power LEDs off, `kernel=kernel612.img`, `initramfs initramfs612 followkernel` |
| Boot, `cmdline.txt` | `modules-load=dwc2`, `overlayroot=tmpfs:recurse=0` (read-only root), `root=PARTUUID=0d18cc81-02` |
| Card layout | p1 boot (vfat) · p2 rootA (committed slot) · p3 rootB (A/B standby) · p4 `/data`, grown to fill the card on first boot |
| Persistent binds on `/data` | tailscale state, `/etc/bridge`, NetworkManager connections, journal, diagnostics, `/etc/default/bridge-return-audio`, `/etc/default/bridge-agent`, `/etc/default/bridge-net` (the WAN seed), and `/home/pi/flight.txt` → `/data/flight.txt` |
| Journal | `Storage=persistent`, `SystemMaxUse=200M`, **`ReadKMsg=no`**. The `-61` storm cost journald 30–37% of a core; kernel messages remain available in `dmesg` |
| Hardware watchdog | `RuntimeWatchdogSec=15`, `RebootWatchdogSec=2min` |
| Wi-Fi | NetworkManager power save off (`wifi.powersave=2`) and no MAC randomisation |
| Enabled services | bridge-gadget, bridge-feeder-net, bridge-uvcd, bridge-feeder-audio, bridge-return-audio, wifi-guardian, bridge-powertrim, flight-recorder, jitter-sentry, bridge-supervisor, bridge-web, bridge-wifi-portal, bridge-idle-frame, gadget-clean-detach, bridge-ab-healthcheck, bridge-wifi-unblock, bridge-firstdiag, bridge-firstboot, bridge-regen-hostkeys, bridge-identity, tailscaled; timers bridge-agent (15 s) and bridge-watchdog (5 min backstop) |
| Deliberately off | bridge-testpattern, bridge-crackle-sentry, bridge-pitch, bridge-idle-frame.timer (the frame never changes). Stock units masked: userconfig, cloud-init*, rpi-resize, systemd-growfs-root |
| Network watcher (`jitter-sentry`) | **Never switches to the LAN profile** (owner's rule). It moves to WAN only when the audio buffer is under 300 ms and the network is bad, so it does nothing on this WAN-300 card. Switches wait until media is idle. The live buffer escalation (`JITTER_SENTRY_LIVE_RUNG`) is off |
| Flight recorder | Python, one line per second: `HH:MM:SS up= udc= thr= pull=`. The last 500 lines are kept; each line is `fdatasync`'d, and `pull=1` means the camera pump reported within 4 s |
| Status page | `bridge-web` on port 8080. The whole answer is rebuilt at most every 2 s (`_STATUS_TTL`); vcgencmd runs at most once a second; expected fps is read from the USB descriptor |
| Fleet agent | Heartbeat and command pull every 15 s to `https://fleet.scine.online` |
| Signed script updates | Four scripts can be overridden: `bridge-feeder-net.sh`, `bridge-feeder-audio.sh`, `bridge-uvcd.sh`, `bridge-return-audio.sh`. The signature is verified at install **and at every start** against `/etc/netbridge/script-pubkey.pem` on the read-only root. Failures are quarantined and the built-in script is used instead. Since 6f8a60a, deploys `sync` to the card before restarting |
| USB identity | VID `0x0525`, PID `0xa4a2`, serial `0123456789` (**shared by all bridges**, see §10), manufacturer = hostname, product "UVC Gadget", `bcdUSB 0x0200`, composite class `0xEF/0x02/0x01` |

---

## 6. Live verification

| Check (20:34 / 20:49 IST) | Result |
|---|---|
| Streams | Video, voice and return live; 0 restarts on every media service |
| Video to the laptop | 30 fps steady (59–61 frames per 2 s) |
| Mac → Pi packets | 220,272 video and 129,607 voice packets, **0 gaps** |
| Wi-Fi Mac ↔ Pi | Round trip averaged 5.3 ms, worst 10.7 ms |
| Pi | CPU about 40% busy, load about 2, temperature 46 °C, uptime 57 min at 20:49, nothing quarantined |
| Audio | Clock "clean"; return at 48 kHz, no mismatch |
| USB camera misses | ⚠️ 6.6–12 per second (not visible now) |
| Power | ⚠️ Under-voltage 57–67% of the time |

## 7. Estimated latency

| Path | Estimate |
|---|---|
| Video: Mac camera → meeting | About 0.21–0.28 s. Camera frame ~33 ms, encode ~5–10, Wi-Fi ~3, **jitter buffer 100**, decode ~6, hand-off 0–33, USB ~25, laptop display ~33–66 |
| Voice: Mac mic → meeting | About 0.4–0.6 s (300 ms jitter buffer plus device buffers) |
| Return: meeting → Mac | About 0.35–0.7 s (250 ms jitter buffer plus device buffers) |

Voice probably trails the picture by about 0.2–0.3 s. It has not been reported as a problem and was left as is.

---

## 8. What changed 22–24 Sep and why (branch `Everything-good-(sound+audio)`)

| Commit | Change | Why |
|---|---|---|
| 083c725 | App: mesh auth key passed via the environment, not argv | The key was visible in `ps` |
| 40d0192 | Camera EAGAIN log filter; hardware H.264 decoder | The log flood; CPU (the decoder was later reverted) |
| e7487c7 | Status page cached | About 20 program launches per request, 25–33% of a core |
| 514277f | CPU sweep: Python flight recorder, agent reuses the status page, AEC reference branch only when AEC is on, idle-frame timer off | Work that produced nothing (the flight recorder alone was ~11% of a core) |
| 8434651 | journald `ReadKMsg=no`; software decoder; video buffer capped at 100 ms; `gpu_mem=16`; 4 s pull window | The `-61` storm cost 30–37% of a core; the hardware path measured worse; the 300 ms buffer was the lag |
| d469ebd | App: H.264 **Baseline** | 41% less Pi decode CPU at the same quality |
| 37b61e7 / b771b47 | App: 800 kbps; test floor taken from measurement | Smaller keyframe bursts, less CPU |
| 98a6cde | 480×270 end to end, `maxpacket 1024` | One USB packet per slot (superseded by f79ff06) |
| f79ff06 | **424×240 @ 30 fps end to end**, 600 kbps; `_expected_fps` bug fixed | The camera's native rate (no judder), still one packet per slot (75%) |
| 52a161b | WAN profile seeded on `/data`; `jitter-sentry` never switches to LAN | No automatic media restarts on a fresh card; the owner's rule |
| 6f8a60a | USB-miss counter built into the camera service; deploys `sync` before restarting; this document | Make the verified setup the default |

**Why the video froze (measured 2026-09-24).** Everything upstream of the USB link was clean:
- the Mac camera delivered about 30 fps;
- there was 0 packet loss;
- Wi-Fi was at worst about 12 ms;
- the Pi decoded cleanly and handed 30 fps to the gadget.

The fault was on the USB link itself:
- The same freezes appeared in Windows' Camera app.
- The camera pump saw about 57–60 USB completions per second, against a physical maximum of about 40 full frames per second, so transfers were being cut short.
- The stock kernel cancels the whole UVC queue on a `-61` miss.

WaysToGo reached the same conclusion on 16 Sep. Moving the IRQ to CPU 2 reduced the misses but did not remove them.

---

## 9. Operating rules

- **Never switch a bridge to the LAN profile** (`bridge profile lan` or the fleet `profile` command). Cut lag elsewhere.
- **Unplug the meeting laptop before any camera-service (`bridge-uvcd`) restart or deploy.** With a host attached, the USB controller hangs and the Pi reboots. This happened 2 out of 2 times on 2026-09-24, and it also explains the 22 Sep restart → reboot cases.
- **Don't restart media services during a meeting.** A restart of the video feeder alone, while the camera holds `/dev/video40`, crash-loops video. To recover: do a clean boot, or stop uvcd → restart the feeder → start uvcd.
- **Signed updates.**
  1. Run `bash tools/publish-script.sh pi/scripts/<script>`.
  2. Send the fleet command `{"type":"deploy-script","args":{"name":"<script>","source":"https://fleet.scine.online/payloads"},"confirm":true}`.
  3. Check with `{"type":"running"}`. The results of slow commands are sometimes lost.
- **Ask before sending.** A fleet command queued by a tool call runs even if that call is interrupted, so ask first, then send.
- **Reading the USB-miss counter:** `{"type":"read-file","args":{"path":"/run/netbridge-usb-video.txt","lines":12,"tail":true}}`.

## 10. Culprits and open issues (as of 20:49 IST)

| # | Issue | Impact | Next step |
|---|---|---|---|
| 1 | USB camera misses, 6.6–12/s in bursts | The source of the earlier freezes; they can come back with more motion or worse power | Test variant C (CPU 2 reserved for USB), variant B (IRQ on CPU 0), WaysToGo's kernel patch and an MJPEG prototype, measuring each with the counter |
| 2 | Power: under-voltage 57–67% of the time | USB misses; resets on camera restarts and at go-live | GPIO wiring (both 5 V pins 2 and 4, grounds 6 and 14, short thick wires) or the supply |
| 3 | **`main` does not contain this configuration** | A future build from `main` (for example by a collaborator) reverts to 640×360@20, which does not match app 1.4.3 | Merge `Everything-good-(sound+audio)` into `main` (the owner's decision) |
| 4 | The app's self-updater decides by file digest, not by version | It could install an OLDER build if one were published | Add a "never install a lower version" rule in the next app build |
| 5 | All bridges share USB serial `0123456789` | Windows caches per serial, so two bridges on one laptop conflict | Give each bridge its own serial (C-01) |
| 6 | The Mac return player logged 3 "timestamp discontinuity, resyncing" warnings in about 11 minutes | Possible brief skips in return audio | Watch; investigate only if heard |
| 7 | Slow fleet commands can lose their result | A deploy may look "timed out" when it succeeded, or the reverse | Verify with `running`; agent timeout fix (D2) |
| 8 | The test runner scores unittest-style files as "NO RESULT" | 10 passing test files look unscored, and the suite prints "NOT GREEN" | Teach `tools/run-tests.sh` to read unittest's `OK` / `FAILED` |

## 11. Restore or rebuild

- **Restore this exact setup.**
  1. Flash `2.1.0-52a161b` (Desktop › NetBridge-Image, or release `v2.1.0-52a161b`).
  2. With the laptop **unplugged**, deploy the signed camera update `97219e9c`. An image built from this branch already has it built in, so skip this step there.
  3. On the Mac, run `~/Desktop/NetBridge/Launch NetBridge.command` (1.4.3).
  4. Keep the WAN profile.
- **Build an image from this branch.**
  1. Run `gh workflow run build-image.yml --ref 'Everything-good-(sound+audio)' -f draft=true` (keep the quotes: the name has brackets).
  2. Verify both manifest signatures with the OTA public key.
  3. Run `bash tools/image-audit.sh <image.img>`.
- **Build the Mac app.** In `app/netbridge-source`, using the Python 3.11 venv that has PyInstaller, run
  `python build.py --version 1.4.3 --signing-key ~/.netbridge/keys/app-signing-key.pem`. The output goes to `dist/` and is ad-hoc signed, for this Mac only.
- **Run the tests.** `bash tools/run-tests.sh`. Results on 2026-09-24:
  - 495 passed.
  - 10 unittest files pass (`OK`) but show as "NO RESULT" (culprit 8).
  - `test-usb-audio-analysis` needs `numpy`.
  - 2 fail. They fail the same way at `main`'s merge base `c4bfd89`, so they are not caused by this branch:
    - `test-fleet-drift`: a comment-only match is flagged as security drift.
    - `test-app-diagnosis`: camera-release escalation order.

## 12. Where things are

| What | Where |
|---|---|
| This document (Word) | `docs/NetBridge-Working-Configuration-2026-09-24.docx`; the owner's copy is in `~/Downloads` |
| Images | `~/Desktop/NetBridge-Image/` with audit reports: slot 1 is the latest (52a161b, in the Pi now), slots 2–4 are rollbacks |
| Mac app (default) | `~/Desktop/NetBridge/` (1.4.3); older builds in `~/Desktop/NetBridge/Older versions/` |
| Mac app logs | `~/.netbridge-source/logs/` (mesh gaps every 10 s per stream) |
| Pi overrides | `/data/overrides/` (signed); quarantine in `/data/overrides/quarantine` |
| Pi USB-miss counter | `/run/netbridge-usb-video.txt` |
| Pi flight recorder | `/home/pi/flight.txt` → `/data/flight.txt` |
| Keys (Mac) | `~/.netbridge/keys/` (app, OTA and script signing keys; never in the repo) |
