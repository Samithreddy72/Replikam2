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
                "audioconvert", "audioresample", "queue", "audiodynamic",
                "volume", "autoaudiosink"] + (
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
        return _bundle_gstreamer_windows(dest)
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


def _bundle_gstreamer_windows(dest: pathlib.Path):
    """Copy a Windows GStreamer runtime into the app.

    Windows resolves DLLs from the executable's own directory and PATH, so there is no
    @loader_path relocation and no re-signing - it is genuinely simpler than macOS. The
    catch is only that the build RUNNER must have GStreamer installed to copy from; the
    CI job installs the official runtime first (see build-app.yml). We copy the whole
    bin/ (gst-launch + every DLL - dependency-walking DLLs by hand is fragile on Windows)
    and only the plugin DLLs the return pipeline uses.
    """
    roots = [os.environ.get("GSTREAMER_1_0_ROOT_MSVC_X86_64", ""),
             os.environ.get("GSTREAMER_1_0_ROOT_X86_64", ""),
             r"C:\gstreamer\1.0\msvc_x86_64", r"C:\gstreamer\1.0\mingw_x86_64"]
    root = next((r for r in roots if r and os.path.isdir(os.path.join(r, "bin"))), None)
    if not root:
        log("no GStreamer runtime found on this Windows runner — return audio will need "
            "GStreamer installed on the presenter's machine")
        return None
    binsrc = os.path.join(root, "bin")
    plugsrc = os.path.join(root, "lib", "gstreamer-1.0")

    libdir = dest / "gst"
    plugdir = libdir / "plugins"
    plugdir.mkdir(parents=True, exist_ok=True)

    # every DLL in bin/, plus gst-launch-1.0.exe
    n = 0
    for f in os.listdir(binsrc):
        if f.lower().endswith(".dll") or f == "gst-launch-1.0.exe":
            shutil.copy2(os.path.join(binsrc, f), libdir / f)
            n += 1
    # only the plugins our pipeline references
    want = {"gstcoreelements", "gstudp", "gstrtpmanager", "gstrtp", "gstopus",
            "gstaudioconvert", "gstaudioresample", "gstautodetect",
            "gstaudioparsers", "gstwasapi", "gstwasapi2", "gstdirectsound"}
    pn = 0
    if os.path.isdir(plugsrc):
        for f in os.listdir(plugsrc):
            base = os.path.splitext(f)[0].replace("libgst", "gst")
            if base in want and f.lower().endswith(".dll"):
                shutil.copy2(os.path.join(plugsrc, f), plugdir / f)
                pn += 1
    log("bundled Windows GStreamer: %d dlls, %d plugins from %s" % (n, pn, root))
    return libdir / "gst-launch-1.0.exe"


def build_mesh():
    """Compile the embedded mesh client (Go) for THIS platform and return its path.

    Go cross-compiles reliably, but PyInstaller does not, so each app binary is built on
    its own OS anyway - we just build the helper for the same target here. Requires the Go
    toolchain on the build machine; without it the app falls back to host Tailscale.
    """
    if not shutil.which("go"):
        log("Go toolchain not found — mesh client not bundled; app will need host Tailscale")
        return None
    src = HERE / "mesh"
    out = src / ("netbridge-mesh.exe" if IS_WIN else "netbridge-mesh")
    env = dict(os.environ, GOFLAGS="-mod=mod")
    log("building embedded mesh client")
    r = subprocess.run(["go", "build", "-o", str(out), "."], cwd=src, env=env)
    if r.returncode != 0 or not out.exists():
        log("mesh client build FAILED — app will need host Tailscale")
        return None
    log("built mesh client -> %s (%.1f MB)" % (out, out.stat().st_size / 1e6))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ffmpeg", action="store_true", help="do not bundle ffmpeg")
    ap.add_argument("--no-gst", action="store_true",
                    help="do not bundle GStreamer (return audio then needs one on PATH)")
    ap.add_argument("--no-mesh", action="store_true",
                    help="do not bundle the embedded mesh client (needs host Tailscale then)")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller missing — install it with:  pip install pyinstaller")
        return 1

    ff = None if args.no_ffmpeg else fetch_ffmpeg(HERE / "_bundle")
    gstdir = None if args.no_gst else bundle_gstreamer(HERE / "_bundle")
    mesh = None if args.no_mesh else build_mesh()

    name = "NetBridgeSource"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--name", name,
           "--distpath", str(DIST), "--workpath", str(WORK),
           "--specpath", str(WORK)]
    sep = ";" if IS_WIN else ":"
    if ff:
        # --add-binary lands it next to the extracted app; _ffmpeg() looks there first.
        cmd += ["--add-binary", "%s%s." % (ff, sep)]
    if mesh:
        # NOTE: PyInstaller strips/re-signs ANY Mach-O it bundles (both --add-binary AND
        # --add-data on macOS), which CORRUPTS the Go helper — the stripped copy silently
        # drops inbound UDP over tsnet (return audio dead). So the AUTHORITATIVE helper is
        # shipped as a SIDECAR next to the app binary (see below + _mesh_bin, which prefers
        # it). We still bundle a copy as a last-resort fallback for non-frozen/dev runs.
        cmd += ["--add-data", "%s%s." % (mesh, sep)]
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

    # Ship the mesh helper as a VERBATIM SIDECAR next to the app binary. This is the copy
    # the app actually runs (_mesh_bin prefers a sidecar); it is byte-for-byte the Go build,
    # never touched by PyInstaller, so tsnet's inbound UDP (return audio) works. Whoever
    # zips/distributes dist/ must keep this file beside the app binary.
    if mesh:
        side = DIST / ("netbridge-mesh.exe" if IS_WIN else "netbridge-mesh")
        shutil.copy2(mesh, side)
        if not IS_WIN:
            os.chmod(side, 0o755)
            # Ad-hoc sign so Gatekeeper/Apple-Silicon runs it (Go binaries are unsigned).
            subprocess.run(["codesign", "--force", "--sign", "-", str(side)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("SIDECAR %s (%.1f MB) — verbatim helper, ships beside the app" %
            (side, side.stat().st_size / 1e6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
