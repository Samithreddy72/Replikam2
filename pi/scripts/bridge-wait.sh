#!/bin/bash
what="$1"; n=0
while [ $n -lt 30 ]; do
  case "$what" in
    video40) [ -e /dev/video40 ] && exit 0 ;;
    uac2)    [ -d /proc/asound/UAC2Gadget ] && exit 0 ;;
  esac
  sleep 0.5; n=$((n+1))
done
exit 0
