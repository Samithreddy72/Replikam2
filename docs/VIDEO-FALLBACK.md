# Video fallback candidate — 29 September 2026

This source candidate is not a certified Pi image or native presenter release. No live device has been changed. The current electrical fault remains outside software's ability to correct.

## Room contract

Only decoded live video, the last complete decoded frame, or limited-range YUY2 black is generated. No text, status artwork or grey fallback. Static slides remain fresh while frames continue decoding. Frame age uses CLOCK_MONOTONIC. At exactly 60 seconds without decoded video, the pump switches to black. Stop, lock, expiry and replacement invalidate the cached session at the next output frame after the Pi receives the event. A Stop that cannot reach the Pi cannot clear it remotely; the local timeout remains in force.

The receiver publishes one complete frame atomically in tmpfs, with its session epoch and decode timestamp. The pump independently checks the current session and deadline, including after a receiver crash. A replacement session closes and rebuilds the video pipeline to discard decoder and socket queues. RTP SSRC binds incoming video to a discriminator derived from the current PIN ticket. This discriminator is public and is not cryptographic authentication; the encrypted mesh and existing IP gate still enforce the transport boundary.

The receiver waits for complete H264 access units/keyframes after loss, bounds jitter and downstream queues, and retains the software decoder and 100 ms video buffer cap. Presenters already emit one keyframe per second. Video errors never deliberately restart voice/return audio or the USB gadget. The observer watchdog no longer defeats systemd restart budgets. Media services have bounded restart bursts. Mesh forward sockets repair independently with three attempts, preserving the running mesh identity; software-queued packets older than 100 ms are discarded instead of replayed after repair.

Complete Pi power loss or loss of the UVC process itself cannot guarantee continuous output. The room app's behavior when its camera disappears is outside Pi control. A pump restart can recover an unexpired current-session snapshot, but this does not eliminate a USB interruption.

## Deployment boundary

Deploy the matching presenter and whole Pi image as a coordinated release. Old presenters use an arbitrary SSRC and cannot send video to this receiver. New presenters remain compatible with the older receiver. The public PIN state advertises `video_binding: ssrc-sha256-v1`.

The image builder and legacy setup now compile the reviewed UVC source. The receiver, PIN gate and pump must ship together; a feeder-only signed script update is not sufficient. Retained binaries under restore are not the new implementation. The candidate requires Python GI and the GStreamer introspection packages installed by the image builder.

## Verification and remaining scope

Executable C tests cover the exact freeze/black boundary, static frames, malformed snapshots, future timestamps, session handover, Stop, expiry and pump restart. Real GStreamer/UDP tests cover H264 decode, static content freshness, sender loss and stale-session RTP. Native mesh race tests inject socket failures and stale queued media. Software checks do not verify the meeting laptop's actual display, cadence, audio or Pi CPU/power margin.

Still required: Linux compilation of the entire UVC binary, full current revision CI, candidate image/native installers, spare-card USB acceptance on Windows/macOS and power qualification. Stable identity across app relaunch with revocation, adaptive video quality, complete device hotplug parity, sleep/wake consent and remaining signed rollback/backup acceptance are not closed by this fallback change. Physical UPS/uplink work is deferred per the owner.
