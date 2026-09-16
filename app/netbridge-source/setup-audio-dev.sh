#!/usr/bin/env bash
# Install source-development GI bindings and isolate the media plugin search path.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
command -v brew >/dev/null || { echo 'This helper requires Homebrew on macOS.' >&2; exit 1; }
brew install gstreamer pygobject3
PYTHON_BIN="$(brew --prefix python@3.14)/bin/python3.14"
"$PYTHON_BIN" -m venv --system-site-packages "$APP_DIR/.venv-gi"
"$APP_DIR/.venv-gi/bin/python" -m pip install certifi
PLUGIN_SOURCE="$(brew --prefix gstreamer)/lib/gstreamer-1.0"
PLUGIN_TARGET="$APP_DIR/_bundle/gi-plugins"
mkdir -p "$PLUGIN_TARGET"
# Avoid scanning unrelated video/GUI plugins at audio startup.
for name in coreelements udp rtp rtpmanager opus audioconvert audioresample audiofx volume osxaudio autodetect audiotestsrc; do
    test -f "$PLUGIN_SOURCE/libgst$name.dylib"
    ln -sfn "$PLUGIN_SOURCE/libgst$name.dylib" "$PLUGIN_TARGET/libgst$name.dylib"
done
GST_PLUGIN_SYSTEM_PATH_1_0="$PLUGIN_TARGET" GST_REGISTRY="$APP_DIR/_bundle/gi-registry.bin" \
    "$APP_DIR/.venv-gi/bin/python" -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
