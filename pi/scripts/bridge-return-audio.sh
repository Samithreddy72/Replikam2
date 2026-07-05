#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
[ -f /etc/default/bridge-return-audio ] && . /etc/default/bridge-return-audio
DEST_IP="${RETURN_DEST_IP:-192.168.29.49}"
DEST_PORT="${RETURN_DEST_PORT:-5004}"
# hw: (not plughw) = no plug-layer resampler noise. Safe to use because the gadget advertises a
# single 48k rate (uvc-raw-setup.sh c_srate=48000), so the client always sends 48k and hw: can
# never desync (no rate-lock cascade). audioresample quality=10 is a no-op at 48->48 but a
# high-quality safety net. opusenc audio-type=generic (the gst default = OPUS_APPLICATION_AUDIO,
# music-optimized; gst's name for ffmpeg's "application=audio") @128k is transparent for music
# (64k was the rate, not the mode, that hurt); inband-fec keeps Tailscale/WAN loss concealed.
exec gst-launch-1.0 alsasrc device=hw:UAC2Gadget buffer-time=200000 latency-time=20000 ! queue max-size-time=300000000 leaky=downstream ! audioconvert ! audioresample quality=10 ! audio/x-raw,rate=48000,channels=2,format=S16LE ! opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20 ! rtpopuspay pt=97 ! udpsink host="$DEST_IP" port="$DEST_PORT" sync=false
