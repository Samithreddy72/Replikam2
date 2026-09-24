#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
exec gst-launch-1.0 videotestsrc is-live=true pattern=ball ! video/x-raw,format=YUY2,width=480,height=270,framerate=20/1 ! v4l2sink device=/dev/video40
