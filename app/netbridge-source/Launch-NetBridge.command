#!/bin/bash
# NetBridge Source — double-click to launch. Your browser opens at http://127.0.0.1:8765 .
# Keep this window open while you present; close it to quit.
#
# Runs via Terminal on purpose: macOS grants camera/mic permission to THIS window, so the
# app inherits it. Launching the same code from anywhere else gets a black camera.
#
# WHY THIS RUNS FROM SOURCE INSTEAD OF THE PACKAGED BINARY
# --------------------------------------------------------
# The PyInstaller one-file build (./NetBridgeSource) is SIGKILLed by macOS on Apple Silicon:
#     EXC_BAD_ACCESS  SIGKILL (Code Signature Invalid)
#     termination: namespace CODESIGNING, code 2, "Invalid Page"
# The outer binary's ad-hoc signature verifies fine on disk — the failure is on the images
# it UNPACKS to a temp dir at runtime. So it starts, serves the UI, answers the API, and is
# then killed the moment it loads its bundled media libraries, i.e. exactly when you press
# Go live. SIGKILL cannot be caught, which is why the window just died with no traceback.
# Proper fix is a signed/notarised build (needs the Apple Developer account); until then
# running from source sidesteps packaging entirely and uses the ffmpeg/GStreamer already on
# this machine. It also means you always run the CURRENT code with no rebuild step.
set -u
REPO="$HOME/netbridge/replikam2-ci"
APP="$REPO/app/netbridge-source/source_app.py"
PY="$REPO/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

[ -f "$APP" ] || { echo "Cannot find $APP"; echo "Press any key to close."; read -r -n1; exit 1; }

echo "Starting NetBridge Source…  (leave this window open; close it to quit)"
echo
exec "$PY" "$APP"
