#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
exec stdbuf -oL -eL /usr/local/bin/uvc-gadget -d /dev/video40 uvc.0
