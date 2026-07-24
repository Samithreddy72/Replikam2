#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
[ -f /etc/default/bridge-return-audio ] && . /etc/default/bridge-return-audio
# No hardcoded fallback IP. 192.168.29.49 was one developer's laptop on one LAN in one
# month; on every other card it meant the bridge quietly streamed the client's audio to a
# stranger's address on the local network and reported no error. If no peer is set, send
# nowhere and say so - the /api/checks return_audio probe is what surfaces it.
DEST_IP="${RETURN_DEST_IP:-}"
DEST_PORT="${RETURN_DEST_PORT:-5004}"
if [ -z "$DEST_IP" ]; then
  echo "bridge-return-audio: no RETURN_DEST_IP set (run: bridge set-peer <ip>) - not streaming" >&2
  exec sleep infinity
fi
# hw: (not plughw) = no plug-layer resampler noise. Safe to use because the gadget advertises a
# single 48k rate (uvc-raw-setup.sh c_srate=48000), so the client always sends 48k and hw: can
# never desync (no rate-lock cascade). audioresample quality=10 is a no-op at 48->48 but a
# high-quality safety net. opusenc audio-type=generic (the gst default = OPUS_APPLICATION_AUDIO,
# music-optimized; gst's name for ffmpeg's "application=audio") @128k is transparent for music
# (64k was the rate, not the mode, that hurt); inband-fec keeps Tailscale/WAN loss concealed.
exec gst-launch-1.0 alsasrc device=hw:UAC2Gadget buffer-time=200000 latency-time=20000 ! queue max-size-time=300000000 leaky=downstream ! audioconvert ! audioresample quality=10 ! audio/x-raw,rate=48000,channels=2,format=S16LE ! opusenc bitrate=128000 audio-type=generic inband-fec=true packet-loss-percentage=20 ! rtpopuspay pt=97 ! udpsink host="$DEST_IP" port="$DEST_PORT" sync=false
