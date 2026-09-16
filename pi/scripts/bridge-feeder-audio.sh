#!/bin/bash
# PHASE 7 (walkthrough J3): the presenter must not hear their own voice come back.
#
# Echo cancellation needs TWO signals: the near-end capture (the room, on its way to the
# presenter) and a FAR-END REFERENCE - what we are playing out, i.e. the presenter's own
# voice going into the meeting laptop. GStreamer's canceller finds its reference through a
# probe registered IN THE SAME PROCESS, but playback and capture are separate services here.
#
# The reference branch MUST be S16BE. RTP L16 is big-endian by RFC 3551, so rtpL16pay
# refuses S16LE outright:
#     could not link audioresample1 to rtpl16pay0, rtpl16pay0 can't handle caps
#     audio/x-raw, rate=48000, channels=2, format=S16LE
# gst-launch builds the WHOLE pipeline or none of it, so one unlinkable element in this
# optional branch killed the alsasink branch too - the presenter's voice never reached the
# meeting, and systemd respawned the failure every 2s (restart counter hit 16 before it was
# caught). An echo-cancellation helper the project has DEFERRED took the live audio path
# down with it. audioconvert ahead of the caps does the byte swap.
#
# So this tee sends a copy of exactly what reaches the speaker to a LOCAL udp port, and the
# return service can consume it as the reference. If the reference branch stalls, the
# leaky queue drops it rather than back-pressuring the leg that matters.
export PATH=/usr/local/bin:/usr/bin:/bin
[ -f /etc/default/bridge-net ] && . /etc/default/bridge-net

# One voice gain controller, with a -6 dBFS target. Suppression is off while diagnosing lost syllables.
# The old S16LE volume=6 stage clipped BEFORE the following compressor could act.
# Do not append integer gain after this limiter or add a default boost at the sender.
# Echo cancellation remains off; the reference branch alone does not implement AEC.
exec gst-launch-1.0 udpsrc port=5002 caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000" ! rtpjitterbuffer latency=${NET_AUDIO_LATENCY:-120} do-lost=true ! rtpopusdepay ! opusdec plc=true use-inband-fec=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16LE ! webrtcdsp echo-cancel=false high-pass-filter=true noise-suppression=false noise-suppression-level=moderate gain-control=true compression-gain-db=6 target-level-dbfs=6 limiter=true ! audioconvert ! tee name=spk \
  spk. ! queue max-size-time=400000000 max-size-bytes=0 max-size-buffers=0 leaky=downstream ! alsasink device=plughw:UAC2Gadget sync=false buffer-time=200000 latency-time=40000 \
  spk. ! queue max-size-time=200000000 leaky=downstream ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16BE ! rtpL16pay ! udpsink host=127.0.0.1 port=${AEC_REF_PORT:-5006} sync=false async=false
