#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
exec stdbuf -oL -eL /usr/local/bin/uvc-gadget -d /dev/video41 uvc.0
