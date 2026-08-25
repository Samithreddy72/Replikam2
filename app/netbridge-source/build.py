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

# Reverse-DNS bundle identifier. macOS keys TCC permission grants (camera, microphone, local
# network) to this string, so it must stay STABLE across releases: change it and every user is
# re-prompted for permissions they already granted, on the next update.
BUNDLE_ID = "online.scine.netbridge.source"

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
    # WHICH PLUGINS TO COPY.
    #
    # The macOS path resolves this dynamically: it asks gst-inspect which file backs each
    # element in GST_ELEMENTS. Windows kept a SECOND, hand-written list of plugin names, and
    # the two drifted -- gstaudiofx (audiodynamic, used twice as compressor and limiter) and
    # gstvolume (the gain stage) were missing, so a Windows build would have produced an app
    # whose return pipeline could not be constructed at all. Room audio silently absent,
    # everything else apparently fine: exactly the failure this project refused to ship when
    # it declined to bypass the GStreamer checksum.
    #
    # So: ask gst-inspect here too, from the single GST_ELEMENTS list, and keep the static set
    # only as a floor for elements that fail to introspect on a headless runner.
    want = {"gstcoreelements", "gstudp", "gstrtpmanager", "gstrtp", "gstopus",
            "gstaudioconvert", "gstaudioresample", "gstaudiofx", "gstvolume",
            "gstautodetect", "gstaudioparsers",
            "gstwasapi", "gstwasapi2", "gstdirectsound"}
    inspect = os.path.join(binsrc, "gst-inspect-1.0.exe")
    if os.path.exists(inspect):
        resolved = set()
        for el in GST_ELEMENTS:
            try:
                out = subprocess.run([inspect, el], capture_output=True, text=True,
                                     timeout=20).stdout
            except Exception:
                continue
            for line in out.splitlines():
                if "Filename" in line:
                    base = os.path.splitext(os.path.basename(line.split()[-1]))[0]
                    resolved.add(base.replace("libgst", "gst"))
                    break
        if resolved:
            missing = resolved - want
            if missing:
                log("gst-inspect found plugins the static list omits: %s" % sorted(missing))
            want |= resolved

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



ENTITLEMENTS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0"><dict>\n'
    '  <key>com.apple.security.cs.disable-library-validation</key><true/>\n'
    '  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>\n'
    '  <key>com.apple.security.device.camera</key><true/>\n'
    '  <key>com.apple.security.device.audio-input</key><true/>\n'
    '</dict></plist>\n'
)


def finish_mac_bundle(app_path, mesh, version):
    """Make the .app a real, shippable macOS application.

    PyInstaller produces a structurally valid bundle and stops. Three things still have to be
    true before it is something a person can be handed:

      INFO.PLIST USAGE STRINGS. macOS refuses camera and microphone access to a bundle that
      does not declare WHY it wants them, and since Sequoia it also gates local networking.
      Without these the app is denied silently - no prompt, no error, just no video, which is
      the most expensive failure mode this project has had.

      THE SIDECAR MUST BE INSIDE. _mesh_bin() looks for the helper next to sys.executable, and
      inside a bundle that is Contents/MacOS/. The helper is copied VERBATIM and never handed
      to PyInstaller, because PyInstaller re-signs any Mach-O it bundles and the stripped copy
      silently drops inbound UDP over tsnet - return audio dead, nothing else visibly wrong.

      SIGNING ORDER. Nested code first, outer bundle last. Sign the bundle first and the later
      signature of a nested binary invalidates the seal, which macOS reports as a damaged app.
    """
    if not app_path.exists():
        return None
    macos = app_path / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)

    if mesh:
        side = macos / "netbridge-mesh"
        shutil.copy2(mesh, side)
        os.chmod(side, 0o755)
        log("bundle: mesh helper placed at Contents/MacOS/netbridge-mesh")

    plist = app_path / "Contents" / "Info.plist"
    keys = [
        ("NSCameraUsageDescription", "string",
         "NetBridge sends your camera to the meeting room's bridge so the room sees you as "
         "its own webcam."),
        ("NSMicrophoneUsageDescription", "string",
         "NetBridge sends your voice to the meeting room's bridge so the room hears you "
         "through its own speakers."),
        ("NSLocalNetworkUsageDescription", "string",
         "NetBridge finds and talks to your bridge on the local network."),
        ("CFBundleShortVersionString", "string", version),
        ("CFBundleVersion", "string", version),
        ("LSMinimumSystemVersion", "string", "12.0"),
        # The UI is a browser tab the app opens itself; there is no Cocoa window. Without this
        # macOS parks a permanent blank icon in the Dock.
        ("LSUIElement", "bool", "true"),
    ]
    # plistlib, NOT PlistBuddy.
    #
    # The first version shelled out to `PlistBuddy -c "Add :key string <value>"`. PlistBuddy
    # parses that value string, and an apostrophe in "the room's bridge" silently truncated it
    # to EMPTY. Two of the three keys came out blank while the third -- the one with no
    # apostrophe -- was fine, and the build reported success either way.
    #
    # An empty NSCameraUsageDescription is not a cosmetic bug: macOS denies the camera to a
    # bundle with no usage string, without prompting. The app would have shipped, launched,
    # gone live, and delivered no video, with nothing anywhere saying why. Structured data
    # deserves a structured writer.
    import plistlib
    with open(plist, "rb") as fh:
        info = plistlib.load(fh)
    for k, typ, val in keys:
        info[k] = (val == "true") if typ == "bool" else val
    with open(plist, "wb") as fh:
        plistlib.dump(info, fh)

    # Verify what actually landed. A usage string that exists but is empty is worse than one
    # that is missing, because it looks correct in a diff.
    with open(plist, "rb") as fh:
        got = plistlib.load(fh)
    blank = [k for k, typ, _ in keys if typ == "string" and not str(got.get(k, "")).strip()]
    if blank:
        raise SystemExit("[build] FATAL: Info.plist keys are present but EMPTY: %s\n"
                         "        macOS denies camera/microphone access silently in this state."
                         % ", ".join(blank))

    log("bundle: Info.plist carries camera / microphone / local-network usage strings")

    # --- signing ---------------------------------------------------------------------------
    # NETBRIDGE_SIGN_ID is a Developer ID Application identity, e.g.
    #   "Developer ID Application: Some Name (TEAMID)"
    # Without it we ad-hoc sign, which is what Apple Silicon needs in order to execute at all
    # but is NOT distributable: a browser download of an ad-hoc bundle is quarantined and
    # refused. The pipeline is identical either way, so the day an Apple Developer account
    # exists this is one environment variable rather than a rewrite.
    ident = os.environ.get("NETBRIDGE_SIGN_ID", "").strip() or "-"
    adhoc = ident == "-"

    ents = None
    if not adhoc:
        # Hardened Runtime is required for notarization, and a PyInstaller one-file app unpacks
        # and dlopen()s its own dylibs at runtime, which library validation blocks outright.
        # These are the minimum entitlements that permit that while keeping the rest of the
        # hardened runtime. Deliberately NOT disable-executable-page-protection, and
        # deliberately not the blanket allow-dyld-environment-variables.
        ents = WORK / "entitlements.plist"
        ents.parent.mkdir(parents=True, exist_ok=True)
        ents.write_text(ENTITLEMENTS)

    def sign(target, seal_bundle=False):
        cmd = ["codesign", "--force", "--sign", ident]
        if not adhoc:
            cmd += ["--timestamp", "--options", "runtime"]
            if ents:
                cmd += ["--entitlements", str(ents)]
        cmd += [str(target)]
        return subprocess.run(cmd, capture_output=True, text=True)

    # NESTED FIRST. What matters is that nothing nested is signed AFTER the bundle itself.
    for f in sorted(macos.rglob("*")):
        if f.is_file() and os.access(f, os.X_OK) and f.name != app_path.stem:
            sign(f)
    r = sign(app_path, seal_bundle=True)
    if r.returncode != 0:
        log("bundle: codesign FAILED: %s" % (r.stderr or "").strip()[:200])
        return app_path

    v = subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=2",
                        str(app_path)], capture_output=True, text=True)
    log("bundle: signed with %s - verify %s"
        % ("ad-hoc (NOT distributable)" if adhoc else ident,
           "OK" if v.returncode == 0 else "FAILED: " + (v.stderr or "").strip()[:160]))

    if adhoc:
        log("bundle: NOT notarised. A browser download of this bundle WILL be quarantined and")
        log("        Gatekeeper will refuse it. Set NETBRIDGE_SIGN_ID (a Developer ID identity)")
        log("        and NETBRIDGE_NOTARY_PROFILE to produce a distributable build.")
        return app_path

    # --- notarization ----------------------------------------------------------------------
    # notarytool wants an archive, not a directory, and it must be made with ditto: plain `zip`
    # loses symlinks and extended attributes and produces a bundle Apple rejects. The ticket is
    # then STAPLED so the first launch works with no network round-trip.
    profile = os.environ.get("NETBRIDGE_NOTARY_PROFILE", "").strip()
    if not profile:
        log("bundle: signed but NOT notarised (NETBRIDGE_NOTARY_PROFILE unset)")
        return app_path
    zipped = DIST / (app_path.stem + "-notarize.zip")
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app_path), str(zipped)], check=True)
    log("bundle: submitting to the Apple notary service (minutes, not seconds)")
    n = subprocess.run(["xcrun", "notarytool", "submit", str(zipped),
                        "--keychain-profile", profile, "--wait"],
                       capture_output=True, text=True)
    if n.returncode != 0:
        log("bundle: notarization FAILED: %s" % (n.stdout + n.stderr)[-400:])
        return app_path
    st = subprocess.run(["xcrun", "stapler", "staple", str(app_path)],
                        capture_output=True, text=True)
    gk = subprocess.run(["spctl", "-a", "-vv", "-t", "exec", str(app_path)],
                        capture_output=True, text=True)
    tail = (gk.stderr or gk.stdout or "").strip().splitlines()
    log("bundle: notarised; staple %s; Gatekeeper says %s"
        % ("OK" if st.returncode == 0 else "FAILED", tail[-1] if tail else "?"))
    try:
        zipped.unlink()
    except OSError:
        pass
    return app_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ffmpeg", action="store_true", help="do not bundle ffmpeg")
    ap.add_argument("--no-gst", action="store_true",
                    help="do not bundle GStreamer (return audio then needs one on PATH)")
    ap.add_argument("--no-mesh", action="store_true",
                    help="do not bundle the embedded mesh client (needs host Tailscale then)")
    ap.add_argument("--version", help="stamp APP_VERSION (also the version the updater compares)")
    ap.add_argument("--signing-key",
                    help="EC private key (PEM) to sign the update manifest with. Without it "
                         "the build still works, it just publishes no update manifest.")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller missing — install it with:  pip install pyinstaller")
        return 1

    # Stamp the version INTO the source before packaging, so the running app and the
    # update manifest can never disagree about which build this is.
    if args.version:
        import re as _re
        src = HERE / "source_app.py"
        src.write_text(_re.sub(r'^APP_VERSION\s*=\s*"[^"]+"',
                               'APP_VERSION = "%s"' % args.version,
                               src.read_text(encoding="utf-8"), count=1, flags=_re.M),
                       encoding="utf-8")
        log("stamped APP_VERSION = %s" % args.version)

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
    # Pin the update-channel public key INSIDE the app. Without it the updater is inert
    # (fail closed), which is the correct behaviour for an unsigned dev build.
    pub = HERE / "app-pubkey.pem"
    if pub.exists():
        cmd += ["--add-data", "%s%s." % (pub, sep)]

    # certifi's cacert.pem must travel with the app. A frozen build has no OS cert store,
    # so without this every https call to the fleet fails CERTIFICATE_VERIFY_FAILED and the
    # bridge list comes back empty. The import lives inside a function, so name it
    # explicitly rather than relying on PyInstaller's static analysis to spot it.
    cmd += ["--hidden-import", "certifi", "--collect-data", "certifi"]
    if IS_MAC:
        # ALWAYS build the .app, not just when an env var happens to be set.
        #
        # This was `["--windowed"] if os.environ.get("NB_APP_BUNDLE") else []`, and nothing in
        # the repository ever set NB_APP_BUNDLE -- so every release for months shipped two bare
        # Mach-O executables. That matters for three separate reasons:
        #
        #  1. GATEKEEPER. A quarantined bare executable has NO user-recoverable path: macOS
        #     kills it (or hangs it in dyld) with an empty stdout and stderr, and Finder offers
        #     nothing. A quarantined .app gets the supported "Open Anyway" flow in System
        #     Settings > Privacy & Security. Same signature, completely different outcome.
        #  2. TCC IDENTITY. A bare binary has no Info.plist, so it has no camera/microphone
        #     usage strings and no bundle identity: macOS attributes the request to whatever
        #     LAUNCHED it. That is the entire reason this project's launcher carries a warning
        #     never to start the app from another tool -- do so and you get microphone but no
        #     video, with no error and no prompt. A .app owns its own TCC entry.
        #  3. NOTARIZATION. Apple notarises bundles. A signing pipeline that has no bundle to
        #     sign cannot be finished later; with one, it is a credential away.
        #
        # The onefile binary is still produced alongside and remains what the proven
        # Launch-NetBridge.command runs, so nothing that works today stops working.
        cmd += ["--windowed", "--osx-bundle-identifier", BUNDLE_ID]
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

    # Finish the .app: usage strings, the helper INSIDE Contents/MacOS, and signing.
    if IS_MAC:
        appb = DIST / (name + ".app")
        finish_mac_bundle(appb, mesh, args.version or _stamped_version())

    # ---- update manifest (walkthrough J3: "kept current by auto-update") -------------
    # Same trust model as the image OTA: an EC-signed manifest naming a sha256. The app
    # ships the matching PUBLIC key and refuses anything it cannot verify, so the update
    # channel is authenticated even though the app itself is not Apple-signed.
    ver = args.version or _stamped_version()
    rel = DIST / "release" / _plat_tag()
    rel.mkdir(parents=True, exist_ok=True)
    fname = "%s-%s-%s%s" % (name, ver, _plat_tag(), ".exe" if IS_WIN else "")
    shutil.copy2(built, rel / fname)
    man = rel / "manifest.txt"
    man.write_text("version=%s\nsha256=%s\nfile=%s\n" % (ver, _sha256(built), fname), encoding="utf-8")
    if args.signing_key:
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", args.signing_key,
                        "-out", str(man) + ".sig", str(man)], check=True)
        # The app pins this pubkey; publish it next to the build so it can be bundled.
        subprocess.run(["openssl", "pkey", "-in", args.signing_key, "-pubout",
                        "-out", str(DIST / "app-pubkey.pem")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("UPDATE %s signed -> %s" % (ver, rel))
    else:
        log("UPDATE %s manifest written UNSIGNED (%s) — pass --signing-key to publish it; "
            "the app refuses unsigned updates by design" % (ver, rel))
    return 0


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _plat_tag():
    if IS_WIN:
        return "windows"
    if IS_MAC:
        return "macos-arm64" if platform.machine() == "arm64" else "macos-x86_64"
    return "linux"


def _stamped_version():
    """Read APP_VERSION out of the source we just packaged, so the manifest and the binary
    can never disagree about what version this is."""
    import re as _re
    m = _re.search(r'^APP_VERSION\s*=\s*"([^"]+)"',
                   (HERE / "source_app.py").read_text(encoding="utf-8"), _re.M)
    return m.group(1) if m else "0.0.0"


if __name__ == "__main__":
    raise SystemExit(main())
