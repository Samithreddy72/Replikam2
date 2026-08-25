#!/usr/bin/env python3
"""The macOS .app bundle, and the signing pipeline that is waiting on a certificate.

TWO FAILURES THIS GUARDS
------------------------
1. The bundle was never built. build.py gated `--windowed` on NB_APP_BUNDLE, and nothing in
   the repository ever set it, so every release for months shipped two bare Mach-O executables.
   A quarantined bare executable has NO user-recoverable path -- macOS kills it or hangs it in
   dyld with empty stdout and stderr. A quarantined .app at least gets "Open Anyway".

2. The usage strings came out EMPTY. The first implementation used PlistBuddy, whose value
   argument is parsed -- an apostrophe in "the room's bridge" truncated two of the three keys
   to nothing, while the third (no apostrophe) was fine, and the build reported success.
   macOS denies the camera to a bundle with an empty NSCameraUsageDescription and does not
   prompt. The app would have gone live and delivered no video, silently.

Static checks always run. The built-artifact checks run only when dist/ has a bundle, so this
file is still meaningful on a CI runner that has not built one.
"""
import pathlib, plistlib, re, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _source import code_only, has_code

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_RAW = (ROOT / "app/netbridge-source/build.py").read_text()
# Search CODE, not prose. Every check below that looks for the ABSENCE of something would
# otherwise match the comment explaining why it was removed -- which is exactly what happened
# on the first run of this file, three separate times.
BUILD = code_only(BUILD_RAW)
APPDIR = ROOT / "app/netbridge-source/dist/NetBridgeSource.app"
P = F = 0


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


print("NetBridge macOS bundle + signing pipeline")
print("=========================================\n")

print("  ---- the bundle must be built, not gated behind an unset variable ----")
if re.search(r"NB_APP_BUNDLE", BUILD):
    no("build still consults NB_APP_BUNDLE",
       "nothing sets it, so the .app silently never gets built")
else:
    ok("no NB_APP_BUNDLE gate remains")

if re.search(r'cmd \+= \["--windowed"', BUILD):
    ok("--windowed is passed unconditionally on macOS")
else:
    no("PyInstaller is not asked to produce a .app")

if "--osx-bundle-identifier" in BUILD and re.search(r'BUNDLE_ID\s*=\s*"[a-z0-9.]+"', BUILD):
    ok("a stable reverse-DNS bundle identifier is set")
else:
    no("bundle identifier must be explicit and stable",
       "macOS keys TCC grants to it; changing it re-prompts every user")

print("\n  ---- usage strings must be written with a structured writer ----")
if "PlistBuddy" in BUILD and "plistlib" not in BUILD:
    no("Info.plist is written with PlistBuddy",
       "its value argument is parsed; an apostrophe silently truncates the string to empty")
elif "plistlib" in BUILD:
    ok("Info.plist is written with plistlib, not by shelling out")
else:
    no("cannot tell how Info.plist is written")

for k in ("NSCameraUsageDescription", "NSMicrophoneUsageDescription",
          "NSLocalNetworkUsageDescription"):
    if k in BUILD:
        ok("%s is declared" % k)
    else:
        no("%s missing" % k, "macOS denies the permission silently without it")

if re.search(r"blank\s*=\s*\[k for k", BUILD) and "EMPTY" in BUILD:
    ok("the build FAILS if a usage string lands empty")
else:
    no("an empty usage string must abort the build",
       "it looks correct in a diff and denies the camera at runtime")

print("\n  ---- signing order and entitlements ----")
i_nested = BUILD.find("for f in sorted(macos.rglob")
i_outer = BUILD.find("r = sign(app_path")
if i_nested != -1 and i_outer != -1 and i_nested < i_outer:
    ok("nested binaries are signed BEFORE the bundle is sealed")
else:
    no("signing order is wrong",
       "sealing the bundle first, then signing something inside it, produces a 'damaged app'")

if "disable-library-validation" in BUILD:
    ok("entitlements allow a one-file bundle to load its own unpacked dylibs")
else:
    no("hardened runtime will block PyInstaller's runtime dylib loading without it")

for forbidden in ("disable-executable-page-protection", "allow-dyld-environment-variables"):
    if forbidden in BUILD:
        no("over-broad entitlement present: %s" % forbidden,
           "only the minimum needed to notarize should be requested")
    else:
        ok("does not request %s" % forbidden)

if "NETBRIDGE_SIGN_ID" in BUILD and "notarytool" in BUILD and "stapler" in BUILD:
    ok("Developer ID signing + notarization + stapling are wired, awaiting credentials")
else:
    no("the notarization path is incomplete")

if re.search(r'ditto", "-c", "-k", "--keepParent"', BUILD):
    ok("notarization archive is made with ditto (zip loses symlinks and xattrs)")
else:
    no("must use ditto to build the notarization archive")

if re.search(r"NOT distributable", BUILD):
    ok("an ad-hoc build says plainly that it is not distributable")
else:
    no("an ad-hoc build must not imply it is shippable")

print("\n  ---- the built artifact, if one is present ----")
if not APPDIR.exists():
    print("  (no dist/NetBridgeSource.app here - static checks above still applied)")
else:
    info = plistlib.loads((APPDIR / "Contents/Info.plist").read_bytes())
    for k in ("NSCameraUsageDescription", "NSMicrophoneUsageDescription",
              "NSLocalNetworkUsageDescription"):
        v = str(info.get(k, "")).strip()
        if len(v) > 20:
            ok("%s is present and non-empty (%d chars)" % (k, len(v)))
        else:
            no("%s is empty or too short in the built bundle" % k,
               "this is the PlistBuddy apostrophe failure returning")

    mesh = APPDIR / "Contents/MacOS/netbridge-mesh"
    if mesh.exists():
        ok("the mesh helper is inside Contents/MacOS (where _mesh_bin looks)")
        side = ROOT / "app/netbridge-source/dist/netbridge-mesh"
        if side.exists():
            import hashlib
            h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            if h(mesh) == h(side):
                ok("the bundled helper is byte-identical to the verbatim sidecar")
            else:
                no("the bundled helper differs from the sidecar",
                   "a re-signed helper silently drops inbound UDP over tsnet - return audio dies")
    else:
        no("no mesh helper inside the bundle", "return audio will not work from the .app")

    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(APPDIR)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok("codesign --verify --deep --strict passes")
    else:
        no("bundle signature does not verify", (r.stderr or "").strip()[:160])

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
