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
# ADAPTIVE PRO CHAIN (webrtcdsp): voice-band HPF + noise suppression (voice-only)
# + true AGC (quiet lifted / loud compressed, adapts in real time) + limiter.
# echo-cancel OFF (headphones handle echo). Fallback: .proven.bak (Jun-27 manual chain)
exec gst-launch-1.0 udpsrc port=5002 caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000" ! rtpjitterbuffer latency=${NET_AUDIO_LATENCY:-120} do-lost=true ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16LE ! webrtcdsp echo-cancel=false high-pass-filter=true noise-suppression=true noise-suppression-level=high gain-control=false limiter=true ! volume volume=6.0 ! audiodynamic mode=compressor characteristics=hard-knee ratio=0.05 threshold=0.95 ! audioconvert ! tee name=spk \
  spk. ! queue max-size-time=400000000 max-size-bytes=0 max-size-buffers=0 leaky=downstream ! alsasink device=plughw:UAC2Gadget sync=false buffer-time=200000 latency-time=40000 \
  spk. ! queue max-size-time=200000000 leaky=downstream ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16LE ! rtpL16pay ! udpsink host=127.0.0.1 port=${AEC_REF_PORT:-5006} sync=false async=false
