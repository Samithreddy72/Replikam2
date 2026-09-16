#!/bin/bash
set -euo pipefail
# Runtime-only trial: the normal installed module remains the reboot fallback.
# Do not hot-swap DWC2 while a host is connected.
state=$(cat /sys/class/udc/fe980000.usb/state)
if [ "$state" != "not attached" ]; then
  echo "Disconnect the USB data cable first (UDC state: $state)." >&2
  exit 1
fi
exec > >(tee -a /data/diagnostics/uvc-module-trial.log) 2>&1
PS4='+ ${SECONDS}s: '
set -x
MODULE=/data/diagnostics/netbridge-uvc-module/usb_f_uvc.ko
G=/sys/kernel/config/usb_gadget/g1
[ "$(uname -r)" = '6.12.93+rpt-rpi-v8' ]
[ "$(modinfo -F vermagic "$MODULE")" = "$(modinfo -F vermagic usb_f_uvc)" ]
[ "$(cat "$G/functions/uac2.usb0/c_chmask")" = 3 ]
ACTIVE=()
for service in bridge-watchdog.timer bridge-supervisor jitter-sentry flight-recorder bridge-feeder-net bridge-feeder-audio bridge-return-audio bridge-uvcd; do
  if systemctl is-active --quiet "$service"; then ACTIVE+=("$service"); fi
done
restore_services() {
 for ((i=${#ACTIVE[@]}-1;i>=0;i--)); do systemctl start "${ACTIVE[i]}" || true; done
}
rollback() {
 set +e
 /usr/local/bin/bridge-gadget-down.sh
 modprobe -r usb_f_uvc
 modprobe usb_f_uvc
 /usr/local/bin/bridge-gadget-setup.sh
 restore_services
 echo 'Restored installed UVC driver' >&2
}
trap rollback EXIT
for service in "${ACTIVE[@]}"; do systemctl stop "$service"; done
systemctl stop bridge-watchdog.service
/usr/local/bin/bridge-gadget-down.sh
[ ! -d "$G" ]
modprobe -r usb_f_uvc
# modprobe -r also unloads unused dependencies; insmod does not reload them.
IFS=, read -ra dependencies <<< "$(modinfo -F depends "$MODULE")"
for dependency in "${dependencies[@]}"; do modprobe "$dependency"; done
insmod "$MODULE"
/usr/local/bin/bridge-gadget-setup.sh
[ "$(cat "$G/UDC")" = fe980000.usb ]
[ "$(cat "$G/functions/uac2.usb0/c_chmask")" = 3 ]
restore_services
systemctl is-active --quiet bridge-uvcd
systemctl is-active --quiet bridge-feeder-audio
trap - EXIT
printf 'Running UVC srcversion: '
cat /sys/module/usb_f_uvc/srcversion
printf 'Audio capture channels: '
cat "$G/functions/uac2.usb0/c_chmask"
