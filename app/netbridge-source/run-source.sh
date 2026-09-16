#!/usr/bin/env bash
# Run the Python app with reusable media binaries; no app bundle is built.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
export NB_MEDIA_DIR="${NB_MEDIA_DIR:-$APP_DIR/_bundle/runtime}"
DEFAULT_PYTHON="$APP_DIR/.venv/bin/python"
if [ -x "$APP_DIR/.venv-gi/bin/python" ]; then DEFAULT_PYTHON="$APP_DIR/.venv-gi/bin/python"; fi
PYTHON_BIN="${NB_PYTHON:-$DEFAULT_PYTHON}"
# GI and plugins must come from the SAME GStreamer installation. Loading the
# relocated release's dylibs into a Homebrew GI process can crash at startup.
if [ -d "$APP_DIR/_bundle/gi-plugins" ]; then
    export GST_PLUGIN_SYSTEM_PATH_1_0="$APP_DIR/_bundle/gi-plugins"
    export GST_PLUGIN_PATH="$GST_PLUGIN_SYSTEM_PATH_1_0"
    export GST_REGISTRY="$APP_DIR/_bundle/gi-registry.bin"
fi
for dependency in "$PYTHON_BIN" "$NB_MEDIA_DIR/ffmpeg" "$NB_MEDIA_DIR/gst/gst-launch-1.0" "$NB_MEDIA_DIR/netbridge-mesh"; do
    if [ ! -x "$dependency" ]; then
        echo "Missing executable: $dependency" >&2
        echo "See README.md: Running from source." >&2
        exit 1
    fi
done
exec "$PYTHON_BIN" -u "$APP_DIR/source_app.py" "$@"
