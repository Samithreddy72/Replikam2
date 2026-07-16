#!/bin/bash
# Renders the in-camera status frame -> /etc/bridge/idle-frame.raw (YUYV 320x180).
# The video pump shows this whenever no presenter stream is arriving, so the
# meeting laptop sees the NetBridge status card instead of black/gray.
# Rendering lives in render-idle-frame.py (PIL) — gst-launch textoverlay was
# quoting-fragile and failed silently (kept a stale frame while claiming success).
exec /usr/bin/python3 /usr/local/bin/render-idle-frame.py
