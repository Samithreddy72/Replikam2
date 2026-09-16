#!/bin/bash
# Opt-in 15-second raw USB audio diagnostic; temporarily pauses only return audio.
set -euo pipefail
exec 9>/run/netbridge-audio-capture.lock
flock -n 9 || { echo 'Another capture is already running'; exit 1; }
RATE=$(amixer -c UAC2Gadget cget "iface=PCM,name='Capture Rate'" | sed -n 's/^ *: *values=\([0-9]*\).*/\1/p' | head -1)
case "$RATE" in 32000|44100|48000) ;; *) echo 'Meeting laptop is not playing USB audio; capture not started.'; exit 2 ;; esac
MASK=$(cat /sys/kernel/config/usb_gadget/g1/functions/uac2.usb0/c_chmask)
case "$MASK" in 1) CHANNELS=1;; 3) CHANNELS=2;; *) echo 'Unsupported USB capture channel mask'; exit 2;; esac
mountpoint -q /data || exit 1
umask 077
DIR=$(mktemp -d /data/diagnostics/alsa-capture-XXXXXXXX)
printf '%s\n' "$RATE" > "$DIR/rate.txt"
printf '%s\n' "$CHANNELS" > "$DIR/channels.txt"
vcgencmd get_throttled > "$DIR/power-before.txt" || true
WAS_ACTIVE=0
systemctl is-active --quiet bridge-return-audio.service && WAS_ACTIVE=1
restore() {
    systemctl unmask --runtime bridge-return-audio.service >/dev/null 2>&1 || true
    if [ "$WAS_ACTIVE" = 1 ]; then systemctl start bridge-return-audio.service; fi
}
# Do not remove an operator's pre-existing runtime mask during cleanup.
[ ! -e /run/systemd/system/bridge-return-audio.service ] && [ ! -L /run/systemd/system/bridge-return-audio.service ] || { echo 'Existing runtime override; refusing capture'; exit 1; }
trap restore EXIT
trap 'exit 130' INT TERM HUP
systemctl mask --runtime bridge-return-audio.service
systemctl stop bridge-return-audio.service
STATUS=0
timeout --signal=INT --kill-after=3 20 arecord -D hw:UAC2Gadget -t wav -f S16_LE -c "$CHANNELS" -r "$RATE" -d 15 "$DIR/pre-encode.wav" 2>"$DIR/arecord.log" || STATUS=$?
printf '%s\n' "$STATUS" > "$DIR/exit-code.txt"
vcgencmd get_throttled > "$DIR/power-after.txt" || true
printf 'CAPTURE_DIRECTORY=%s\nCAPTURE_EXIT=%s\n' "$DIR" "$STATUS"
exit "$STATUS"
