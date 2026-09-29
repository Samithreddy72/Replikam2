# Video fallback candidate — 29 September 2026

This source candidate is not a certified Pi image or native presenter release. No live device has been changed. The current electrical fault remains outside software's ability to correct.

## Room contract

Only decoded live video, the last complete decoded frame, or limited-range YUY2 black is generated. No text, status artwork or grey fallback. Static slides remain fresh while frames continue decoding. Frame age uses CLOCK_MONOTONIC. At exactly 60 seconds without decoded video, the pump switches to black. Stop, lock, expiry and replacement invalidate the cached session at the next output frame after the Pi receives the event. A Stop that cannot reach the Pi cannot clear it remotely; the local timeout remains in force.

The receiver publishes one complete frame atomically in tmpfs, with its session epoch and decode timestamp. The pump independently checks the current session and deadline, including after a receiver crash. A replacement session closes and rebuilds the video pipeline to discard decoder and socket queues. RTP SSRC binds incoming video to a discriminator derived from the current PIN ticket. This discriminator is public and is not cryptographic authentication; the encrypted mesh and existing IP gate still enforce the transport boundary.

The receiver waits for complete H264 access units/keyframes after loss, bounds jitter and downstream queues, and retains the software decoder and 100 ms video buffer cap. Presenters already emit one keyframe per second. Video errors never deliberately restart voice/return audio or the USB gadget. The observer watchdog no longer defeats systemd restart budgets. Media services have bounded restart bursts. Mesh forward sockets repair independently in bursts of three attempts with a one-minute cooldown, preserving the running mesh identity; software-queued packets older than 100 ms are discarded instead of replayed after repair.

Complete Pi power loss or loss of the UVC process itself cannot guarantee continuous output. The room app's behavior when its camera disappears is outside Pi control. A pump restart can recover an unexpired current-session snapshot, but this does not eliminate a USB interruption.

## Deployment boundary

Deploy the matching presenter and whole Pi image as a coordinated release. Old presenters use an arbitrary SSRC and cannot send video to this receiver. New presenters remain compatible with the older receiver. The public PIN state advertises `video_binding: ssrc-sha256-31-v1`.

The image builder and legacy setup now compile the reviewed UVC source. The receiver, PIN gate and pump must ship together; a feeder-only signed script update is not sufficient. Retained binaries under restore are not the new implementation. The candidate requires Python GI and the GStreamer introspection packages installed by the image builder.

## Verification and remaining scope

Executable C tests cover the exact freeze/black boundary, static frames, malformed snapshots, future timestamps, session handover, Stop, expiry and pump restart. Real GStreamer/UDP tests cover H264 decode, static content freshness, sender loss and stale-session RTP. Native mesh race tests inject socket failures and stale queued media. Software checks do not verify the meeting laptop's actual display, cadence, audio or Pi CPU/power margin.

The initial candidate bf12e1d passed full CI including Linux UVC compilation and native mesh race tests on all three platforms. Subsequent changes require a new run before release.

Additional candidate behavior: video-only adaptation steps 600k/30 fps → 400k/20 fps → 250k/15 fps after sustained measured low decode rate. It waits at least 60 seconds between changes and three minutes of continuous health before increasing quality. USB dimensions and frame cadence stay fixed; audio DSP/jitter is unchanged. Unknown/zero frame rate and known Pi undervoltage do not trigger adaptation. Camera/Windows microphone repairs wait for the selected device instead of choosing an alternative; the existing explicit macOS system-default microphone mode continues to follow the OS choice.

Native sleep notifications stop capture and require explicit Go live after wake. The handlers follow [Apple IOKit notifications](https://developer.apple.com/library/archive/qa/qa1340/_index.html) and [Windows suspend/resume callbacks](https://learn.microsoft.com/en-us/windows/win32/api/powerbase/nf-powerbase-powerregistersuspendresumenotification). Registration/cleanup is testable without sleeping a computer; real lid-close/Modern Standby acceptance still requires the native laptops.

Fleet Docker logs are capped. A daily consistent SQLite backup module keeps 14 verified local copies; an installer enables the timer only after the first backup succeeds. This is local recovery, not protection from losing the Fleet host. Existing encrypted export tooling remains available; off-host destination/retention must be configured and restored in acceptance.

Still required: full latest-revision CI, candidate image/native installers, spare-card USB acceptance on Windows/macOS and power qualification. Persistent mesh identity across app relaunch with revocation, complete physical device-hotplug/sleep acceptance, and remaining signed rollback/off-host backup acceptance are not closed by this change. Physical UPS/uplink work is deferred per the owner.
