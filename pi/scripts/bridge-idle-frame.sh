#!/bin/bash
# Blank black YUYV 640x360 whenever no presenter video is arriving.
# The running USB camera reloads the atomically replaced frame automatically.
exec /usr/bin/python3 /usr/local/bin/render-idle-frame.py
