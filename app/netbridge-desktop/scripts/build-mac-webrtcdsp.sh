#!/usr/bin/env bash
# Build GStreamer's webrtcdsp plugin (webrtcdsp + webrtcechoprobe) for Homebrew's GStreamer.
#
# Homebrew builds gst-plugins-bad without it: there is no webrtc-audio-processing formula, and
# its meson runs with --wrap-mode=nofallback, so the plugin is silently skipped. Without it the
# presenter's echo cancellation cannot run on macOS (see audio_engine.ECHO_PROBE) and the room
# keeps hearing itself whenever a Mac presenter uses speakers.
#
# Builds ONLY that plugin, from the very source Homebrew built the installed GStreamer from
# (checksum-pinned by the formula, so it always matches the installed libraries - GStreamer's
# dylib compatibility versions refuse a plugin built from newer sources), with
# webrtc-audio-processing and abseil linked in statically from GStreamer's own pinned wrap.
# The single dylib goes next to Homebrew's other plugins, where PyInstaller's Gst hook picks it
# up like any other. Usage: build-mac-webrtcdsp.sh [work-dir]
set -euo pipefail
test "$(uname -s)" = Darwin
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
brew install meson ninja pkgconf >/dev/null

work="${1:-${RUNNER_TEMP:-$(mktemp -d)}/webrtcdsp}"
rm -rf "$work"
mkdir -p "$work"
python="$(brew --prefix python@3.14)/bin/python3.14"   # the Python pygobject3 serves
gst_prefix="$(brew --prefix gstreamer)"
installed="$(pkg-config --modversion gstreamer-1.0)"
formula="$(brew info --json=v2 gstreamer | "$python" -c 'import json, sys; print(json.load(sys.stdin)["formulae"][0]["versions"]["stable"])')"
if [ "$installed" != "$formula" ]; then
  echo "Installed GStreamer $installed is not the formula's $formula: a plugin built from" \
       "different sources may not load. Reinstall gstreamer first." >&2
  exit 1
fi
echo "Building webrtcdsp for Homebrew GStreamer $installed"

# Homebrew verifies the formula's pinned sha256 while fetching.
brew fetch --build-from-source --retry gstreamer
source="$(brew --cache --build-from-source gstreamer)"
test -f "$source"
top="gstreamer-$installed"
tar -xf "$source" -C "$work" "$top/subprojects/gst-plugins-bad" "$top/subprojects/webrtc-audio-processing.wrap"
bad="$work/$top/subprojects/gst-plugins-bad"
mkdir -p "$bad/subprojects"
cp "$work/$top/subprojects/webrtc-audio-processing.wrap" "$bad/subprojects/"   # git wrap, pinned revision

# Only webrtcdsp. webrtc-audio-processing (and the abseil it carries) come from their wraps
# even if a system copy exists, and link statically: Homebrew's shared abseil would tie the
# plugin to whichever abseil release Homebrew ships next. auto_features also reaches the
# subproject, so its NEON code path has to be switched back on explicitly.
cd "$bad"
meson setup build --prefix="$gst_prefix" --libdir=lib --buildtype=release \
  --wrap-mode=nofallback --force-fallback-for=webrtc-audio-processing,abseil-cpp \
  -Dauto_features=disabled -Dwebrtcdsp=enabled \
  -Dwebrtc-audio-processing:default_library=static \
  -Dwebrtc-audio-processing:neon=enabled
ninja -C build ext/webrtcdsp/libgstwebrtcdsp.dylib

# Installed beside coreelements: the directory PyInstaller's Gst hook collects plugins from.
plugins="$("$python" -c 'import gi, os; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(os.path.dirname(Gst.Registry.get().find_plugin("coreelements").get_filename()))')"
plugin="$work/libgstwebrtcdsp.dylib"
cp build/ext/webrtcdsp/libgstwebrtcdsp.dylib "$plugin"
chmod u+w "$plugin"
# Its own install name is @rpath/libgstwebrtcdsp.dylib; give it the final location (as
# Homebrew's plugins have) so only real dependencies remain under @rpath below.
install_name_tool -id "$plugins/libgstwebrtcdsp.dylib" "$plugin"
# The plugin links gst-plugins-bad's audio library as built in this tree (@rpath/...). Point
# it at Homebrew's installed copy of the same version, and drop the build tree's rpaths.
otool -L "$plugin" | awk 'NR > 1 && $1 ~ /^@rpath\// {print $1}' | while read -r dep; do
  target="$gst_prefix/lib/${dep#@rpath/}"
  test -f "$target" || { echo "No installed library for $dep (looked for $target)" >&2; exit 1; }
  install_name_tool -change "$dep" "$target" "$plugin"
done
otool -l "$plugin" | awk '$1 == "cmd" && $2 == "LC_RPATH" {getline; getline; print $2}' | while read -r rpath; do
  install_name_tool -delete_rpath "$rpath" "$plugin"
done
codesign --force --sign - "$plugin"
otool -L "$plugin"
if otool -L "$plugin" | tail -n +2 | grep -E "@rpath|absl|webrtc-audio|$work"; then
  echo "libgstwebrtcdsp.dylib still depends on the build tree or a shared abseil/webrtc library" >&2
  exit 1
fi

cp "$plugin" "$plugins/"
echo "Installed $plugins/libgstwebrtcdsp.dylib"

# Prove it loads and actually processes: a probe and a canceller bound to it, run to EOS.
"$python" - <<'PY'
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
for name in ('webrtcdsp', 'webrtcechoprobe'):
    assert Gst.ElementFactory.find(name), name + ' did not register'
pipeline = Gst.parse_launch(
    'audiotestsrc num-buffers=100 samplesperbuffer=480 ! audio/x-raw,format=S16LE,rate=48000,channels=2 '
    '! webrtcechoprobe name=reference ! fakesink sync=false '
    'audiotestsrc num-buffers=100 samplesperbuffer=480 wave=pink-noise ! audio/x-raw,format=S16LE,rate=48000,channels=1 '
    '! webrtcdsp probe=reference echo-cancel=true gain-control=false noise-suppression=false ! fakesink sync=false')
pipeline.set_state(Gst.State.PLAYING)
message = pipeline.get_bus().timed_pop_filtered(20 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
pipeline.set_state(Gst.State.NULL)
if message is None or message.type != Gst.MessageType.EOS:
    raise SystemExit('webrtcdsp pipeline failed: %s' % (message.parse_error()[0].message if message else 'timed out'))
print('webrtcdsp and webrtcechoprobe work with', Gst.version_string())
PY
