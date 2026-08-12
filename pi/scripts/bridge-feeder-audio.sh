#!/bin/bash
# PHASE 7 (walkthrough J3): the presenter must not hear their own voice come back.
#
# Echo cancellation needs TWO signals: the near-end capture (the room, on its way to the
# presenter) and a FAR-END REFERENCE - what we are playing out, i.e. the presenter's own
# voice going into the meeting laptop. GStreamer's canceller finds its reference through a
# probe registered IN THE SAME PROCESS, but playback and capture are separate services here.
#
# So this tee sends a copy of exactly what reaches the speaker to a LOCAL udp port, and the
# return service consumes it as the reference. The alsasink branch is untouched: the audio
# the meeting hears is bit-identical to before, and if the reference branch stalls, the
# leaky queue drops it rather than back-pressuring the leg that matters.
export PATH=/usr/local/bin:/usr/bin:/bin
[ -f /etc/default/bridge-net ] && . /etc/default/bridge-net

# WAIT FOR A USB HOST BEFORE TOUCHING THE GADGET SOUND CARD.
#
# alsasink writes to plughw:UAC2Gadget, which only accepts audio once a host has CONFIGURED
# the audio interface. With no laptop plugged in the sink cannot open, gst exits, and
# Restart=always brings the whole service back two seconds later - forever. Observed
# 2026-08-12: bridge-feeder-audio cycling activating->active every ~2s with nothing attached,
# which burned CPU every two seconds on a board with power problems, filled the journal, and
# raised a service_down alert on each pass (18 emails in 20 minutes).
#
# Waiting here instead means the unit sits quietly ACTIVE until a laptop appears, then starts
# the pipeline once. systemd sees a healthy long-running service either way, which is the
# truth: it IS running, waiting for a client.
#
# No timeout on purpose - a bridge with nothing plugged in should wait indefinitely, not fail.
# Polling once a second costs nothing next to respawning a GStreamer pipeline at the same rate.
udc_configured() {
  for st in /sys/class/udc/*/state; do
    [ -e "$st" ] || continue
    [ "$(cat "$st" 2>/dev/null)" = "configured" ] && return 0
  done
  return 1
}
if ! udc_configured; then
  logger -t bridge-feeder-audio "no USB host attached - waiting (not restarting) until one is"
  while ! udc_configured; do sleep 1; done
  logger -t bridge-feeder-audio "USB host attached - starting the audio pipeline"
fi
# ADAPTIVE PRO CHAIN (webrtcdsp): voice-band HPF + noise suppression (voice-only)
# + true AGC (quiet lifted / loud compressed, adapts in real time) + limiter.
# echo-cancel OFF (headphones handle echo). Fallback: .proven.bak (Jun-27 manual chain)
exec gst-launch-1.0 udpsrc port=5002 caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000" ! rtpjitterbuffer latency=${NET_AUDIO_LATENCY:-120} do-lost=true ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16LE ! webrtcdsp echo-cancel=false high-pass-filter=true noise-suppression=true noise-suppression-level=high gain-control=false limiter=true ! volume volume=6.0 ! audiodynamic mode=compressor characteristics=hard-knee ratio=0.05 threshold=0.95 ! audioconvert ! tee name=spk \
  spk. ! queue max-size-time=400000000 max-size-bytes=0 max-size-buffers=0 leaky=downstream ! alsasink device=plughw:UAC2Gadget sync=false buffer-time=200000 latency-time=40000 \
  spk. ! queue max-size-time=200000000 leaky=downstream ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16LE ! rtpL16pay ! udpsink host=127.0.0.1 port=${AEC_REF_PORT:-5006} sync=false async=false
