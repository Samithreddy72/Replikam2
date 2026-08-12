#!/bin/bash
# NetBridge presenter — double-click this.
#
# IMPORTANT: this must be double-clicked (or run from Terminal), never started by another
# tool. macOS attributes a camera request to the RESPONSIBLE process — whatever launched
# the app — not to the app itself. Launched from Terminal you inherit Terminal's camera
# grant; launched by something without one you get microphone but NO camera, with no error
# and no prompt: voice reaches the bridge, video never does, and the green webcam LED
# stays off.

cd "$(dirname "$0")"
URL=http://127.0.0.1:8765/

up(){ curl -s -m 2 "$URL"api/state >/dev/null 2>&1; }

# Already running: just bring it up. The app is not started again, so it will not open a
# second tab of its own.
if up; then
  echo "NetBridge is already running — opening it."
  open "$URL"; sleep 1; exit 0
fi

echo "Starting NetBridge…  (first launch takes ~15s while it unpacks)"
if [ -x ./NetBridgeSource ]; then
  ./NetBridgeSource >/tmp/netbridge-app.log 2>&1 &
else
  python3 "$HOME/netbridge/replikam2-ci/app/netbridge-source/source_app.py" \
    >/tmp/netbridge-app.log 2>&1 &
fi

for i in $(seq 1 40); do up && break; sleep 1; done

if up; then
  # Deliberately NO `open` here. The app opens the browser itself once it is serving
  # (_open_browser in source_app.py). Opening it here as well is what produced TWO tabs
  # on every launch.
  echo
  echo "NetBridge is running.   $URL"
  echo "(You can close this window — NetBridge keeps running.)"
else
  echo; echo "NetBridge did not start. Last lines of its log:"
  tail -20 /tmp/netbridge-app.log
  echo; echo "Press return to close."; read -r _
fi
