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
if [ "${NB_UVC_BUILD_ONLY:-0}" = 1 ]; then
  # No V4L2 implementation is linked: any accidental loopback dependency fails this test.
  cc -Wall -Wextra -Werror -I"$work/uvc-gadget/include/uvcgadget" -I"$work/uvc-gadget/lib" \
    tests/uvc-static-source.c "$work/uvc-gadget/lib/v4l2-source.c" -o "$work/static-source-test"
  "$work/static-source-test"
fi
if [ "${NB_UVC_BUILD_ONLY:-0}" != 1 ]; then
  meson install -C "$work/build"
  ldconfig
fi
