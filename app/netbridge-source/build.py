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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ffmpeg", action="store_true", help="do not bundle ffmpeg")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller missing — install it with:  pip install pyinstaller")
        return 1

    ff = None if args.no_ffmpeg else fetch_ffmpeg(HERE / "_bundle")

    name = "NetBridgeSource"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--name", name,
           "--distpath", str(DIST), "--workpath", str(WORK),
           "--specpath", str(WORK)]
    if ff:
        # --add-binary lands it next to the extracted app; _ffmpeg() looks there first.
        sep = ";" if IS_WIN else ":"
        cmd += ["--add-binary", "%s%s." % (ff, sep)]
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
