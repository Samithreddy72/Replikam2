#!/usr/bin/env bash
# Build the reviewed source, never silently substitute a retained binary.
set -euo pipefail
cd "$(dirname "$0")/.."
work=${1:?supply an empty build directory}
mkdir -p "$work"
[ ! -e "$work/uvc-gadget" ] || { echo 'build directory already populated' >&2; exit 1; }
tar xzf sources/patched-uvc-gadget-sources.tgz -C "$work"
cp sources/v4l2-source-with-idle-frame.c "$work/uvc-gadget/lib/v4l2-source.c"
cp sources/bridge-video-frame.h "$work/uvc-gadget/lib/"
meson setup "$work/build" "$work/uvc-gadget" --prefix=/usr/local --libdir=lib/aarch64-linux-gnu
meson compile -C "$work/build"
if [ "${NB_UVC_BUILD_ONLY:-0}" != 1 ]; then
  meson install -C "$work/build"
  ldconfig
fi
