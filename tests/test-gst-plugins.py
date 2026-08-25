#!/usr/bin/env python3
"""Every GStreamer element the app uses must actually travel with the app.

THE BUG THIS EXISTS TO PREVENT
------------------------------
The return-audio pipeline uses `audiodynamic` twice (compressor, then limiter) and `volume`
once. The Windows bundler's hand-written plugin list contained neither, so a Windows build
would have shipped an app whose return pipeline could not be constructed at all -- the room's
audio silently absent, everything else apparently fine.

That is exactly the failure the project refused to ship when it declined to bypass the
GStreamer checksum, and it was sitting in the bundler the whole time. Nobody had put the
pipeline string and the bundler list side by side.

So this test does that, mechanically, in both directions:
  * every element in the pipeline must be declared in build.py's GST_ELEMENTS
  * every element in GST_ELEMENTS must map to a plugin the Windows floor list ships

It deliberately does NOT require gst-inspect: it must run on any machine, including CI runners
without GStreamer installed, or it will quietly stop guarding anything.
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = (ROOT / "app/netbridge-source/source_app.py").read_text()
BUILD = (ROOT / "app/netbridge-source/build.py").read_text()
P = F = 0


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


# element -> plugin, verified with `gst-inspect-1.0 <element>` on 26 Aug 2026.
# Plugin basenames are identical across macOS and Windows (libgstX.dylib / gstX.dll).
ELEMENT_PLUGIN = {
    "udpsrc": "gstudp",
    "rtpjitterbuffer": "gstrtpmanager",
    "rtpopusdepay": "gstrtp",
    "opusdec": "gstopus",
    "audioconvert": "gstaudioconvert",
    "audioresample": "gstaudioresample",
    "audiodynamic": "gstaudiofx",
    "volume": "gstvolume",
    "queue": "gstcoreelements",
    "autoaudiosink": "gstautodetect",
    "osxaudiosink": "gstosxaudio",
    "wasapisink": "gstwasapi",
    "directsoundsink": "gstdirectsound",
}

print("NetBridge GStreamer plugin coverage")
print("===================================\n")

# --- what the app's pipeline actually references -------------------------------------------
# The pipeline is assembled as a string with ' ! ' separators. Take the first token of each
# stage: that is the element name.
pipe_txt = "\n".join(l for l in APP.splitlines() if "!" in l and "gst" not in l.lower()[:4])
used = set()
for stage in re.split(r"\s!\s", pipe_txt):
    m = re.search(r"([a-z][a-z0-9]{3,})", stage.strip())
    if m and m.group(1) in ELEMENT_PLUGIN:
        used.add(m.group(1))
# belt and braces: also catch any known element mentioned anywhere in the app
for el in ELEMENT_PLUGIN:
    if re.search(r"\b%s\b" % re.escape(el), APP):
        used.add(el)

if used:
    ok("found %d known GStreamer elements referenced by the app" % len(used))
else:
    no("could not find any pipeline elements — this test has stopped guarding anything")

# --- build.py's declared element list -------------------------------------------------------
m = re.search(r"GST_ELEMENTS\s*=\s*\[(.*?)\]\s*\+\s*\(", BUILD, re.S)
declared = set(re.findall(r'"([a-z0-9]+)"', m.group(1))) if m else set()
m2 = re.search(r"GST_ELEMENTS\s*=.*?\n\s*\[(.*?)\]\s*if IS_MAC else\s*\[(.*?)\]", BUILD, re.S)
if m2:
    declared |= set(re.findall(r'"([a-z0-9]+)"', m2.group(1) + m2.group(2)))

if declared:
    ok("build.py declares %d elements in GST_ELEMENTS" % len(declared))
else:
    no("could not parse GST_ELEMENTS from build.py")

print("\n  ---- every element the app uses must be declared for bundling ----")
gap = sorted(used - declared)
if not gap:
    ok("every element the pipeline uses is declared in GST_ELEMENTS")
else:
    no("elements used by the app but NOT bundled: %s" % gap,
       "these plugins will be missing from the shipped app and the pipeline will fail to build")

print("\n  ---- the Windows floor list must cover those elements' plugins ----")
mw = re.search(r"want\s*=\s*\{(.*?)\}", BUILD, re.S)
floor = set(re.findall(r'"([a-z0-9]+)"', mw.group(1))) if mw else set()
if floor:
    ok("Windows floor list declares %d plugins" % len(floor))
else:
    no("could not parse the Windows `want` set")

needed = {ELEMENT_PLUGIN[e] for e in declared if e in ELEMENT_PLUGIN}
# osxaudio is macOS-only and legitimately absent from a Windows list
needed.discard("gstosxaudio")
miss = sorted(needed - floor)
if not miss:
    ok("Windows floor covers every plugin the declared elements need")
else:
    no("Windows bundle would omit: %s" % miss,
       "a build would succeed and the return pipeline would fail to construct — silent, no room audio")

for critical in ("gstaudiofx", "gstvolume"):
    if critical in floor:
        ok("%s is present (the plugin that was actually missing)" % critical)
    else:
        no("%s missing from the Windows floor list" % critical)

print("\n  ---- and the list should not be the only defence ----")
if "gst-inspect-1.0.exe" in BUILD and "resolved" in BUILD:
    ok("Windows also resolves plugins dynamically via gst-inspect, as macOS does")
else:
    no("Windows still relies solely on a hand-maintained list",
       "that is what drifted; resolve from GST_ELEMENTS instead")

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
