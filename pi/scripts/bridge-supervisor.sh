#!/bin/bash
# Event-driven pipeline supervisor (walkthrough J3: "a camera renegotiation
# restarts the stream in ~2 s"). Blocks on kernel uevents for the video loopback
# and the USB gadget; when one fires it runs the SAME idempotent checks the 30s
# watchdog runs — but within ~1-2 s instead of up to 30 s.
#
# Why event-driven and not just a faster poll: rapidly restarting the USB gadget
# hangs dwc2 and can spiral the whole Pi (see bridge-uvcd.service). So we do NOT
# poll faster. We react only to REAL device changes, and a debounce coalesces an
# event burst (a renegotiation emits many events) into a single check so the
# gadget is never churned. bridge-watchdog.timer stays on as a slow backstop.
#
# Net vs the old 30s timer: faster recovery (event push, not poll) AND fewer
# scheduled wakeups (the daemon sleeps in a blocking read at 0% CPU).
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
L(){ logger -t bridge-supervisor "$*"; }

DEBOUNCE="${SUPERVISOR_DEBOUNCE:-3}"   # seconds: never run checks more than once per window
SETTLE="${SUPERVISOR_SETTLE:-1}"       # seconds: let device state settle before checking
last_run=0

run_checks(){
  now=$(date +%s)
  # cooldown — coalesce event storms so we never churn the gadget
  [ $((now - last_run)) -lt "$DEBOUNCE" ] && return 0
  last_run=$now
  L "device event -> running pipeline checks"
  bridge-watchdog.sh
}

# A relevant event is one touching our video loopback or the USB gadget/audio.
relevant(){
  case "$1" in
    *video4linux*|*/gadget/*|*usb_gadget*|*udc*|*UAC2*|*uvc*) return 0 ;;
    *) return 1 ;;
  esac
}

L "supervisor starting (event-driven; watches video4linux + usb gadget)"
# Run once on start in case something is already wrong.
run_checks

# udevadm monitor blocks in-kernel (0% CPU) until an event arrives. If it ever
# exits, the while-loop ends and systemd (Restart=always) relaunches us.
udevadm monitor --udev --subsystem-match=video4linux --subsystem-match=usb 2>/dev/null |
while read -r line; do
  if relevant "$line"; then
    sleep "$SETTLE"
    run_checks
  fi
done
L "udev monitor stream ended — exiting for systemd restart"
