#!/usr/bin/env bash
# macOS sender — equivalent of kreo-stream.bat (which is Windows/dshow only).
# Streams this Mac's camera + mic to the Pi bridge over RTP.
#   Video -> rtp://PI:5000  (H.264, 320x180 @ 20fps)  -> shows up as "UVC Camera"
#   Voice -> rtp://PI:5002  (Opus 48k stereo)         -> shows up as "Microphone (Source/Sink)"
#
# Usage:  ./mac-stream.sh            (uses defaults below)
#         PI=192.168.29.184 VIDEO_DEV=0 AUDIO_DEV=1 ./mac-stream.sh
# List device indexes with:  ffmpeg -f avfoundation -list_devices true -i ""
set -euo pipefail

PI="${PI:-100.91.108.50}"         # Pi's TAILSCALE IPv4 — STABLE on every network (LAN IP keeps
                                  # changing: 192.168.1.13 / 192.168.29.184 / ... as you move WiFi).
                                  # Tailscale establishes a direct LAN path when on the same network
                                  # (low latency) and falls back to the encrypted tunnel otherwise.
                                  # Override for lowest latency on a known LAN:  PI=<pi-lan-ip> ./mac-stream.sh
                                  # mDNS resolves bridge.local to BOTH v4+v6, and ffmpeg's
                                  # RTP muxer prefers IPv6, but the Pi's gst udpsrc is IPv4-only.
                                  # ACROSS NETWORKS: set PI to the Pi's Tailscale 100.x address
                                  # (`tailscale ip -4` on the Pi). The 100.x is a stable literal
                                  # IPv4, so it satisfies the udpsrc constraint and is reachable
                                  # from any network over the encrypted tunnel.
                                  #   PI=100.x.y.z ./mac-stream.sh
VIDEO_DEV="${VIDEO_DEV:-0}"       # avfoundation video index (0 = FaceTime HD Camera)
AUDIO_DEV="${AUDIO_DEV:-1}"       # avfoundation audio index (1 = Bassheads headset mic; go-live auto-detects)
MIC_GAIN_DB="${MIC_GAIN_DB:-0}"   # Pi WebRTC AGC owns voice level; avoid stacked gain.
                                  # Explicit sender gain remains available for unusual inputs.
FPS="${FPS:-20}"                  # output frame rate. 20 = ceiling; 15 is smoother on a shallow
                                  # loopback buffer (steady 15 beats a starving 20).  FPS=15 ./mac-stream.sh

echo ">> Streaming camera[$VIDEO_DEV] + mic[$AUDIO_DEV] (+${MIC_GAIN_DB}dB pre-gain) to $PI @ ${FPS}fps  (Ctrl-C to stop)"

# --- keep the Mac awake while streaming (2026-06-27) ---
# macOS idle-sleep kills the camera, mic AND the return listener mid-call. caffeinate holds the
# whole system + display awake for exactly as long as THIS sender runs (-w $$ exits caffeinate when
# the script does), so a call can never drop just because the Mac dozed. No effect when not streaming.
caffeinate -dimsu -w $$ &

# --- auto-register the return-audio peer (the documented two-sided bring-up) ---
# Tell the Pi to send speaker-return audio back to THIS machine's Tailscale IP, so
# `bridge set-peer` no longer has to be run by hand. Non-fatal; opt out with NO_RETURN_REGISTER=1.
if [ -z "${NO_RETURN_REGISTER:-}" ]; then
  TS_CLI=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
  MY_TS_IP=$("$TS_CLI" ip -4 2>/dev/null | head -1 || true)
  if [ -n "$MY_TS_IP" ]; then
    echo ">> registering return peer $MY_TS_IP with the Pi..."
    ssh -i ~/.ssh/pi_bridge -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new pi@"$PI" "bridge set-peer $MY_TS_IP" \
      || echo ">> WARN: return-peer auto-register failed; run 'bridge set-peer $MY_TS_IP' on the Pi manually"
  else
    echo ">> WARN: no Tailscale IP found locally; skipping return-peer auto-register"
  fi
fi

# --- mic -> Pi :5002 (background, AUTO-RESTART on glitch — survives network blips) ---
# Was fire-once: a brief network outage (e.g. Pi switching Wi-Fi) killed the mic ffmpeg and it
# never came back, so forward audio went dead while video (which has its own loop) kept working.
# Now the mic is wrapped in the same restart loop as the camera below.
( mic_n=0; while true; do
    # FORWARD voice: known-good `-application lowdelay` Opus. (A 2026-06-27 attempt to add ffmpeg
    # Opus FEC `-fec 1` broke this — ffmpeg's RTP muxer rejects the FEC stream, "Invalid argument",
    # mic crash-loops. For sender FEC use a GStreamer opusenc sender, NOT ffmpeg.) Loss resilience
    # is handled on the PI: opusdec plc=true + rtpjitterbuffer do-lost=true conceal dropped packets.
    mic_t=$SECONDS
    ffmpeg -hide_banner -loglevel warning \
      -f avfoundation -i "none:${AUDIO_DEV}" \
      -af "volume=${MIC_GAIN_DB}dB,alimiter=limit=0.9:level=false" \
      -c:a libopus -b:a 64k -ar 48000 -ac 2 -application lowdelay \
      -payload_type 97 -f rtp "rtp://${PI}:5002" || true
    mic_n=$((mic_n+1)); mic_d=$(( SECONDS - mic_t ))
    # Crash-loop detector: a healthy mic runs for minutes. If ffmpeg keeps EXITING within seconds,
    # the command/permission is broken — surface it loudly instead of silently respawning a dead
    # stream. Either way it auto-restarts fast (1s), so a real glitch just blips and recovers.
    if [ "$mic_d" -lt 3 ]; then
      echo ">> ⚠ MIC exited after ${mic_d}s (auto-restart #$mic_n). If this repeats fast: check"
      echo ">>   System Settings > Privacy > Microphone > Terminal (and that the mic command is valid)."
    else
      echo ">> mic stream ended after ${mic_d}s — auto-restarting (#$mic_n)..."
    fi
    sleep 1
  done ) &
MIC_PID=$!
trap 'kill $MIC_PID 2>/dev/null; pkill -P $MIC_PID 2>/dev/null || true' EXIT

# --- camera -> Pi :5000 (foreground, auto-restart on glitch like the .bat) ---
while true; do
  # -xerror: exit on decode error so the loop below restarts (instead of freezing).
  # -video_size 1280x720: PIN the capture mode. The FaceTime camera renegotiates its
  # native format mid-stream (Center Stage / Reactions / Continuity), which crashes a
  # fixed-size raw input. 720p is true 16:9 so it also scales to 320x180 without squish.
  # HARDWARE H.264 encode (h264_videotoolbox, added 2026-06-27): software libx264 burned ~36% CPU,
  # which starved the return-audio playback on this same Mac -> return jitter. The Apple media-engine
  # encoder does the same job at ~3% CPU, freeing the CPU so return audio stays smooth. dump_extra
  # inserts SPS/PPS at each keyframe so the Pi's rtph264depay/avdec_h264 can sync (videotoolbox
  # doesn't repeat headers inline by default). Revert to the libx264 block in git history if HW fails.
  ffmpeg -hide_banner -loglevel warning -xerror \
    -f avfoundation -framerate 30 -video_size 1280x720 -pixel_format uyvy422 -i "${VIDEO_DEV}:none" \
    -vf "scale=320:180,format=nv12" -fps_mode cfr -r "$FPS" \
    -c:v h264_videotoolbox -realtime 1 -b:v 400k -g "$FPS" \
    -bsf:v dump_extra=freq=keyframe -an \
    -f rtp "rtp://${PI}:5000?pkt_size=1100" || true
  echo ">> video pipeline ended, restarting in 2s..."
  sleep 2
done
