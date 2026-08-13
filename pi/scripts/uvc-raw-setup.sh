#!/bin/sh
# SPDX-License-Identifier: MIT

set -e

CONFIGFS="/sys/kernel/config"
GADGET="$CONFIGFS/usb_gadget"
VID="0x0525"
PID="0xa4a2"
SERIAL="0123456789"
MANUF=$(hostname)
PRODUCT="UVC Gadget"
BOARD=$(strings /proc/device-tree/model)
UDC=$(ls /sys/class/udc) # will identify the 'first' UDC

# ---------------------------------------------------------------- audio tuning overrides
# /data/gadget-tuning.conf lets the two USB-audio parameters that actually matter be changed
# WITHOUT rebuilding and reflashing an image. They can only be set here, at boot, before the
# function is linked into a configuration — configfs refuses them afterwards, and trying it on
# a running bridge took this bridge down twice on 2026-08-13. So the file is read here, once,
# and applied before anything is linked.
#
# Format, one KEY=VALUE per line:
#     UAC2_C_SYNC=async          # or adaptive
#     UAC2_REQ_NUMBER=32         # 2..64
#
# The file is NOT sourced. It is running as root at boot, and sourcing a writable file would
# make /data a place where anyone able to write it could execute arbitrary code. Only known
# keys are read, and only values that pass validation are used — anything else falls back to
# the built-in default and says so in the log, because a typo must never leave the bridge
# without working audio.
TUNE_FILE=/data/gadget-tuning.conf
tune_get() {   # tune_get <KEY> <default>
	_v=""
	if [ -f "$TUNE_FILE" ]; then
		_v=$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$TUNE_FILE" 2>/dev/null \
		     | tail -1 | cut -d= -f2- | tr -d ' \t"'"'"'')
	fi
	[ -n "$_v" ] && echo "$_v" || echo "$2"
}
tune_sync() {
	_s=$(tune_get UAC2_C_SYNC "${UAC2_C_SYNC:-adaptive}")
	case "$_s" in
		async|adaptive) echo "$_s" ;;
		*) echo "  tuning: ignoring c_sync='$_s' (expected async|adaptive)" >&2; echo adaptive ;;
	esac
}
tune_reqnum() {
	_r=$(tune_get UAC2_REQ_NUMBER "${UAC2_REQ_NUMBER:-32}")
	case "$_r" in
		''|*[!0-9]*) echo "  tuning: ignoring req_number='$_r' (not a number)" >&2; echo 32 ;;
		*) if [ "$_r" -ge 2 ] && [ "$_r" -le 64 ]; then echo "$_r"
		   else echo "  tuning: ignoring req_number='$_r' (expected 2..64)" >&2; echo 32; fi ;;
	esac
}

echo "Detecting platform:"
echo "  board : $BOARD"
echo "  udc   : $UDC"

create_frame() {
	# Example usage:
	# create_frame <function name> <width> <height> <format> <name>

	FUNCTION=$1
	WIDTH=$2
	HEIGHT=$3
	FORMAT=$4
	NAME=$5

	wdir=functions/$FUNCTION/streaming/$FORMAT/$NAME/${HEIGHT}p

	mkdir -p $wdir
	echo $WIDTH > $wdir/wWidth
	echo $HEIGHT > $wdir/wHeight
	echo $(( $WIDTH * $HEIGHT * 2 )) > $wdir/dwMaxVideoFrameBufferSize
	# Advertise ONLY the real rate: 500000 (100ns units) = 20fps. The old list also had
	# 100000 (=100fps) and 5000000 (=2fps); the kernel sorts ascending so 100000 became the
	# DEFAULT, and webcam apps negotiated ~100fps against a ~20fps source -> the gadget had a
	# fresh frame only every ~5th poll -> severe judder. One matching rate = smooth motion.
	# (Sender must run at 20fps to match — mac-stream.sh default; FPS=20.)
	cat <<EOF > $wdir/dwFrameInterval
500000
EOF
}

create_uvc() {
	# Example usage:
	#	create_uvc <target config> <function name>
	#	create_uvc config/c.1 uvc.0
	CONFIG=$1
	FUNCTION=$2

	echo "	Creating UVC gadget functionality : $FUNCTION"
	mkdir functions/$FUNCTION

	create_frame $FUNCTION 640 360 uncompressed u

	mkdir functions/$FUNCTION/streaming/header/h
	cd functions/$FUNCTION/streaming/header/h
	ln -s ../../uncompressed/u
	cd ../../class/fs
	ln -s ../../header/h
	cd ../../class/hs
	ln -s ../../header/h
	cd ../../class/ss
	ln -s ../../header/h
	cd ../../../control
	mkdir header/h
	ln -s header/h class/fs
	ln -s header/h class/ss
	cd ../../../

	# Include an Extension Unit if the kernel supports that
	if [ -d functions/$FUNCTION/control/extensions ]; then
		mkdir functions/$FUNCTION/control/extensions/xu.0
		pushd functions/$FUNCTION/control/extensions/xu.0

		# Set the bUnitID of the Processing Unit as the XU's source
		echo 2 > baSourceID

		# Set this XU as the source for the default output terminal
		cat bUnitID > ../../terminal/output/default/bSourceID

		# Flag some arbitrary controls. This sets alternating bits of the
		# first byte of bmControls active.
		echo 0x55 > bmControls

		# Set the GUID
		echo -e -n "\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10" > guidExtensionCode

		popd
	fi

	# Set the packet size: uvc gadget max size is 3k...
	echo 2048 > functions/$FUNCTION/streaming_maxpacket

	ln -s functions/$FUNCTION configs/c.1
}

delete_uvc() {
	# Example usage:
	#	delete_uvc <target config> <function name>
	#	delete_uvc config/c.1 uvc.0
	CONFIG=$1
	FUNCTION=$2

	echo "	Deleting UVC gadget functionality : $FUNCTION"
	rm $CONFIG/$FUNCTION

	rm functions/$FUNCTION/control/class/*/h
	rm functions/$FUNCTION/streaming/class/*/h
	rm functions/$FUNCTION/streaming/header/h/u
	rmdir functions/$FUNCTION/streaming/uncompressed/u/*/
	rmdir functions/$FUNCTION/streaming/uncompressed/u
	rm -rf functions/$FUNCTION/streaming/mjpeg/m/*/
	rm -rf functions/$FUNCTION/streaming/mjpeg/m
	rmdir functions/$FUNCTION/streaming/header/h
	rmdir functions/$FUNCTION/control/header/h
	rmdir functions/$FUNCTION
}

case "$1" in
    start)
	echo "Creating the USB gadget"

	echo "Creating gadget directory g1"
	mkdir -p $GADGET/g1

	cd $GADGET/g1
	if [ $? -ne 0 ]; then
	    echo "Error creating usb gadget in configfs"
	    exit 1;
	else
	    echo "OK"
	fi

	echo "Setting Vendor and Product ID's"
	echo $VID > idVendor
	echo $PID > idProduct
	echo 0x0200 > bcdUSB
	echo 0xEF > bDeviceClass
	echo 0x02 > bDeviceSubClass
	echo 0x01 > bDeviceProtocol
	echo "OK"

	echo "Setting English strings"
	mkdir -p strings/0x409
	echo $SERIAL > strings/0x409/serialnumber
	echo $MANUF > strings/0x409/manufacturer
	echo $PRODUCT > strings/0x409/product
	echo "OK"

	echo "Creating Config"
	mkdir configs/c.1
	mkdir configs/c.1/strings/0x409

	echo "Creating functions..."
	create_uvc configs/c.1 uvc.0
	echo "OK"

	echo "Creating UAC2 microphone function..."
	mkdir functions/uac2.usb0
	# ADAPTIVE sync for the host->gadget (speaker) stream: the default "async"
	# mode requires a feedback endpoint that Windows' usbaudio2.sys must honor —
	# a documented source of drift -> stutter-burst -> multi-second stream resets
	# on continuous audio (music), while pause-laden speech self-heals. Adaptive
	# is what real USB soundcards use: no feedback dance, host streams steadily.
	C_SYNC=$(tune_sync)
	echo "  tuning: c_sync=$C_SYNC  req_number=$(tune_reqnum)  (edit $TUNE_FILE to change)"
	echo "$C_SYNC" > functions/uac2.usb0/c_sync || true
	# Deeper URB queue (default 2) rides out dwc2 scheduling latency.
	# IN-FLIGHT USB REQUESTS. At a 1ms service interval this is literally how many
	# milliseconds the gadget can absorb the driver being late before audio is LOST — not
	# delayed, lost, because an isochronous slot that is missed is gone.
	#
	# 8 was not enough. Measured on this hardware while streaming with the meeting laptop as
	# host and NO camera in use: 12 of 33 sample windows short of frames, 0.6ms of audio
	# never captured per second of stream, worst single window ~5ms. Gaps of 2-5ms against
	# 8ms of headroom is exactly what running out of queued requests looks like.
	#
	# dwc2 makes this worse than it sounds: it has no hardware (u)frame tracking, so any
	# periodic endpoint forces the driver to unmask SOF interrupts — measured elsewhere at
	# 250-300k interrupts/sec — and unlike dwc3 it has no interrupt moderation to blunt them.
	# We cannot fix the controller, but we can give it a deeper runway.
	#
	# 32 = 32ms of tolerance for ~96-byte mono packets: a few KB of memory, no latency cost
	# (these are queued requests, not added buffering in the audio path).
	# See raspberrypi/linux#5188 and the CM4 gadget interrupt-load thread.
	echo "$(tune_reqnum)" > functions/uac2.usb0/req_number || true
	echo 1 > functions/uac2.usb0/c_chmask
	# Return path (the "Speakers/Source" the client plays into): SINGLE 48 kHz rate.
	#
	# It was briefly 48000,44100,32000 for walkthrough phase 6 ("at whatever rate the
	# meeting laptop happens to play"). That produced audible jitter, and capturing the
	# return stream to a file showed why: 29 runs of EXACT ZEROS >=1ms in 21s, all at ~1ms
	# granularity — the USB frame interval. The gadget was dropping isochronous frames and
	# the driver was zero-filling them; opus then encoded the damage faithfully, which is
	# why every transport measurement (0 packet loss, perfect RTP timestamps) looked clean.
	#
	# THAT CONCLUSION WAS OVERTURNED — restored to multi-rate 2026-08-12.
	#
	# The measurement above was taken with the v1 rate-following SUPERVISOR in place, and the
	# same commit's own first conclusion was that the supervisor is the dominant fault: a
	# shell loop stopping and restarting gst underneath a live ALSA capture, 100x worse than
	# not having it (21.6 vs 0.2 zero-runs/sec). Multi-rate was NEVER measured without it, so
	# "multi-rate is also worse" (1.4/sec) was never isolated from the supervisor's damage.
	#
	# Phase 6 v2 replaced that supervisor with the kernel's own Capture Rate control — rate
	# following INSIDE the pipeline, which is exactly what the revert said it had to be — and
	# the multi-rate descriptor was then deployed to Samith's working card by card surgery.
	# It has run there since, and he confirmed clean audio at 32k, 44.1k and 48k by ear.
	#
	# PROOF, from that card's own diagnostics bundle on 2026-08-12 (gadget-av.txt):
	#     c_sync = adaptive
	#     c_srate = 48000,44100,32000
	#
	# The work was never committed, so every image built since has silently regressed to a
	# single rate — and flashing one overwrites a working multi-rate card. Windows then offers
	# no choice of format, because the device is advertising that it has none. This restores
	# the configuration that is proven on hardware.
	#
	# If audio ever degrades after a change here, measure it the way the revert did: capture
	# the return stream to a file and count runs of EXACT ZEROS. 0.2/sec is clean, 21.6/sec is
	# broken. Transport metrics cannot see this damage — it happens at the gadget, before opus.
	echo 48000,44100,32000 > functions/uac2.usb0/c_srate
	echo 2 > functions/uac2.usb0/c_ssize
	echo 3 > functions/uac2.usb0/p_chmask
	echo 48000,44100,32000 > functions/uac2.usb0/p_srate
	echo 2 > functions/uac2.usb0/p_ssize
	ln -s functions/uac2.usb0 configs/c.1/
	echo "OK"

	echo "Binding USB Device Controller"
	echo $UDC > UDC
	echo "OK"
	;;

    stop)
	echo "Stopping the USB gadget"

	set +e # Ignore all errors here on a best effort

	cd $GADGET/g1

	if [ $? -ne 0 ]; then
	    echo "Error: no configfs gadget found"
	    exit 1;
	fi

	echo "Unbinding USB Device Controller"
	grep $UDC UDC && echo "" > UDC
	echo "OK"

	delete_uvc configs/c.1 uvc.0

	echo "Clearing English strings"
	rmdir strings/0x409
	echo "OK"

	echo "Cleaning up configuration"
	rmdir configs/c.1/strings/0x409
	rmdir configs/c.1
	echo "OK"

	echo "Removing gadget directory"
	cd $GADGET
	rmdir g1
	cd /
	echo "OK"
	;;
    *)
	echo "Usage : $0 {start|stop}"
esac
