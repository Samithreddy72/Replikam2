#!/usr/bin/env python3
"""Package NetBridge Source into a single self-contained executable.

Produces one file per platform that a presenter can download and run with nothing
installed — no Python, no ffmpeg (walkthrough J3 step 1: "the app brings everything").

    python3 build.py            # build for the platform you are on
    python3 build.py --no-ffmpeg  # skip bundling ffmpeg (much smaller, needs one on PATH)

PyInstaller cannot cross-compile: a macOS build must run on macOS and a Windows build on
Windows. That is why .github/workflows/build-app.yml runs this on both runners — it is the
only way to produce a Windows binary without owning a Windows machine.

NOT code-signed. On first run the recipient has to get past their OS:
  * macOS   — right-click the app -> Open (once), or: xattr -dr com.apple.quarantine <app>
  * Windows — "More info" -> "Run anyway" on the SmartScreen prompt
Signing removes those prompts and needs a paid Apple account / EV certificate; it is
deliberately out of scope here.
"""
import argparse, os, platform, shutil, subprocess, sys, urllib.request, zipfile, tarfile
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
DIST = HERE / "dist"
WORK = HERE / "build"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Static ffmpeg builds. Bundling means the presenter installs nothing; it also pins the
# ffmpeg we tested against rather than whatever happens to be on their machine.
FFMPEG_URLS = {
    "Darwin-arm64": "https://www.osxexperts.net/ffmpeg71arm.zip",
    "Darwin-x86_64": "https://www.osxexperts.net/ffmpeg71intel.zip",
    "Windows-AMD64": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
}


def log(*a):
    print("[build]", *a, flush=True)


def fetch_ffmpeg(dest: pathlib.Path) -> pathlib.Path | None:
    """Download a static ffmpeg next to the app. Returns the binary path, or None."""
    key = "%s-%s" % (platform.system(), platform.machine())
    url = FFMPEG_URLS.get(key)
    if not url:
        log("no bundled ffmpeg for %s — the app will fall back to one on PATH" % key)
        return None
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
    if out.exists():
        log("ffmpeg already fetched")
        return out
    log("downloading ffmpeg for %s" % key)
    tmp = dest / "ffmpeg-dl"
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as e:
        log("ffmpeg download FAILED (%s) — continuing without a bundled binary" % e)
        return None
    # The archives differ in layout; find the binary wherever it landed.
    try:
        if zipfile.is_zipfile(tmp):
            with zipfile.ZipFile(tmp) as z:
                z.extractall(dest / "_ff")
        else:
            with tarfile.open(tmp) as t:
                t.extractall(dest / "_ff")
    except Exception as e:
        log("could not unpack ffmpeg (%s)" % e)
        return None
    want = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    for root, _dirs, files in os.walk(dest / "_ff"):
        if want in files:
            shutil.copy2(os.path.join(root, want), out)
            out.chmod(0o755)
            log("bundled ffmpeg -> %s (%.1f MB)" % (out, out.stat().st_size / 1e6))
            return out
    log("ffmpeg binary not found inside the archive")
    return None


# The exact elements our return-audio pipeline uses. Bundling only the plugins that
# back these keeps the payload ~20MB instead of shipping all of GStreamer.
GST_ELEMENTS = ["udpsrc", "rtpjitterbuffer", "rtpopusdepay", "opusdec",
                "audioconvert", "audioresample", "autoaudiosink"] + (
                ["osxaudiosink"] if IS_MAC else ["wasapisink", "directsoundsink"])


def _macho_deps(path):
    try:
        out = subprocess.run(["otool", "-L", path], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    deps = []
    for line in out.splitlines()[1:]:
        lib = line.strip().split(" (")[0]
        if lib.startswith(("/opt/", "/usr/local/")):    # never system libs from /usr/lib
            deps.append(lib)
    return deps


def bundle_gstreamer(dest: pathlib.Path):
    """Copy gst-launch-1.0, the plugins we use, and their whole dylib closure.

    The walkthrough promises the presenter installs nothing ("the app brings
    everything"), and return audio needs GStreamer - so it has to travel with the app.

    The subtle part is dylib RELOCATION. Homebrew libraries reference each other by
    ABSOLUTE path, so simply copying them produces a bundle that works on this machine
    (where /opt/homebrew exists) and fails on a clean one. Every install name is rewritten
    to @loader_path, and build.py then verifies with DYLD_PRINT_LIBRARIES that nothing
    outside the bundle is loaded - because that failure is invisible to any test run here.
    """
    if IS_WIN:
        # Windows DLLs resolve from the exe's directory, so bundling there is simpler than
        # macOS relocation - but it needs a GStreamer install on the build runner to copy
        # from, which the CI image does not have. Until that is added the Windows build
        # falls back to ffmpeg for return audio, which is audible but not jitter-buffered.
        log("Windows GStreamer bundling not implemented — return audio uses the ffmpeg fallback")
        return None
    gst = shutil.which("gst-launch-1.0")
    if not gst:
        log("gst-launch-1.0 not found on this machine — cannot bundle it")
        return None

    libdir = dest / "gst"
    plugdir = libdir / "plugins"
    plugdir.mkdir(parents=True, exist_ok=True)

    # locate the plugin .dylib backing each element
    plugins = set()
    for el in GST_ELEMENTS:
        try:
            out = subprocess.run(["gst-inspect-1.0", el], capture_output=True,
                                 text=True, timeout=15).stdout
        except Exception:
            continue
        for line in out.splitlines():
            if "Filename" in line:
                plugins.add(line.split()[-1]); break
    if not plugins:
        log("could not resolve any GStreamer plugins — skipping")
        return None

    # walk the closure
    seen, queue = {}, [gst] + sorted(plugins)
    while queue:
        src = queue.pop()
        if not src or src in seen or not os.path.exists(src):
            continue
        seen[src] = True
        queue.extend(_macho_deps(src))

    copied = {}
    for src in seen:
        tgt = (plugdir if src in plugins else libdir) / os.path.basename(src)
        if src == gst:
            tgt = libdir / "gst-launch-1.0"
        if not tgt.exists():
            shutil.copy2(src, tgt)
            tgt.chmod(0o755)
        copied[src] = tgt

    # rewrite every absolute reference to @loader_path so the bundle is self-contained
    for src, tgt in copied.items():
        subprocess.run(["install_name_tool", "-id", "@loader_path/" + tgt.name, str(tgt)],
                       capture_output=True)
        for dep in _macho_deps(src):
            if dep in copied:
                rel = ("@loader_path/../" + copied[dep].name
                       if tgt.parent == plugdir else "@loader_path/" + copied[dep].name)
                subprocess.run(["install_name_tool", "-change", dep, rel, str(tgt)],
                               capture_output=True)
    # RE-SIGN. install_name_tool invalidates the existing signature, and Apple Silicon
    # kills any binary whose signature does not match - the process dies with SIGKILL
    # (exit 137) and prints NOTHING, so it looks like a silent no-op rather than a
    # signing problem. Ad-hoc (-s -) is enough to make the loader accept it.
    if IS_MAC:
        for tgt in copied.values():
            subprocess.run(["codesign", "--force", "--sign", "-", str(tgt)],
                           capture_output=True)
        log("re-signed %d binaries (ad-hoc) after relocation" % len(copied))

    total = sum(os.path.getsize(t) for t in copied.values())
    log("bundled GStreamer: %d files, %.1f MB" % (len(copied), total / 1e6))
    return libdir / "gst-launch-1.0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ffmpeg", action="store_true", help="do not bundle ffmpeg")
    ap.add_argument("--no-gst", action="store_true",
                    help="do not bundle GStreamer (return audio then needs one on PATH)")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller missing — install it with:  pip install pyinstaller")
        return 1

    ff = None if args.no_ffmpeg else fetch_ffmpeg(HERE / "_bundle")
    gstdir = None if args.no_gst else bundle_gstreamer(HERE / "_bundle")

    name = "NetBridgeSource"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--name", name,
           "--distpath", str(DIST), "--workpath", str(WORK),
           "--specpath", str(WORK)]
    sep = ";" if IS_WIN else ":"
    if ff:
        # --add-binary lands it next to the extracted app; _ffmpeg() looks there first.
        cmd += ["--add-binary", "%s%s." % (ff, sep)]
    if gstdir:
        # Ship the whole gst/ tree (binary + libs + plugins). --add-data keeps the layout,
        # which matters because the dylibs reference each other via @loader_path and the
        # plugins sit one level down in plugins/.
        cmd += ["--add-data", "%s%sgst" % (gstdir.parent, sep)]
    if IS_MAC:
        # A .app bundle is what macOS users expect to double-click. The onefile binary
        # still works from a terminal, and is what CI zips.
        cmd += ["--windowed"] if os.environ.get("NB_APP_BUNDLE") else []
    cmd += [str(HERE / "source_app.py")]

    log(" ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        return r.returncode

    built = DIST / (name + (".exe" if IS_WIN else ""))
    if built.exists():
        log("BUILT %s (%.1f MB)" % (built, built.stat().st_size / 1e6))
    else:
        log("expected output missing: %s" % built)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
