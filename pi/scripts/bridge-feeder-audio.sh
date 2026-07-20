#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
[ -f /etc/default/bridge-net ] && . /etc/default/bridge-net
# ADAPTIVE PRO CHAIN (webrtcdsp): voice-band HPF + noise suppression (voice-only)
# + true AGC (quiet lifted / loud compressed, adapts in real time) + limiter.
# echo-cancel OFF (headphones handle echo). Fallback: .proven.bak (Jun-27 manual chain)
exec gst-launch-1.0 udpsrc port=5002 caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000" ! rtpjitterbuffer latency=${NET_AUDIO_LATENCY:-120} do-lost=true ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16LE ! webrtcdsp echo-cancel=false high-pass-filter=true noise-suppression=true noise-suppression-level=high gain-control=true compression-gain-db=24 limiter=true ! volume volume=1.5 ! audiodynamic mode=compressor characteristics=hard-knee ratio=0.05 threshold=0.95 ! audioconvert ! queue max-size-time=400000000 max-size-bytes=0 max-size-buffers=0 leaky=downstream ! alsasink device=plughw:UAC2Gadget sync=false buffer-time=200000 latency-time=40000
