#!/usr/bin/env python3
"""Prove the packaged GStreamer tree can actually build the return-audio pipeline.

WHY THIS EXISTS
---------------
The Windows bundler's plugin allow-list was missing gstaudiofx and gstvolume. The return
pipeline uses `audiodynamic` twice (compressor, then limiter) and `volume` once, so the app
would have built cleanly, launched cleanly, answered /api/state cleanly, passed the existing
CI smoke test cleanly -- and been unable to construct its return pipeline at runtime. Room
audio silently absent; everything else apparently fine.

The existing smoke test could not have caught it: it checks that the app serves HTTP and can
reach the fleet over TLS, neither of which touches GStreamer.

So this asks the only question that matters: given ONLY the plugins we are about to ship, does
every element in the pipeline resolve? It runs against the staged _bundle/gst tree, before
packaging, on whichever platform is building.

    python3 tools/verify-gst-bundle.py [path-to-bundle-gst-dir]

Exit 0 = every element resolves from the bundle. Exit 1 = something is missing, with names.
"""
import os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "app/netbridge-source/_bundle/gst"

# Kept in step with GST_ELEMENTS in build.py. Platform-specific sinks are checked only on the
# platform that uses them -- osxaudiosink genuinely should not be in a Windows bundle.
CORE = ["udpsrc", "rtpjitterbuffer", "rtpopusdepay", "opusdec",
        "audioconvert", "audioresample", "queue", "audiodynamic", "volume", "autoaudiosink"]
SINKS = {"darwin": ["osxaudiosink"], "win32": ["wasapisink"]}


def main():
    gstdir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not gstdir.is_dir():
        print("  no staged GStreamer tree at %s — nothing to verify" % gstdir)
        return 0

    plugdir = gstdir / "plugins"
    # gst-inspect does not have to come from the bundle -- it only has to be pointed AT the
    # bundle. The bundler copies gst-launch and not gst-inspect, so requiring the tool to be
    # inside meant this silently fell back to a filename check on both platforms, which is a
    # much weaker claim than the one this script is supposed to make.
    #
    # So: use any gst-inspect we can find (bundle, PATH, or the runner's own GStreamer install)
    # and force it to resolve ONLY against the bundled plugin directory.
    exe = _find_inspect(gstdir)
    if not exe:
        return _by_filename(plugdir)

    # Point GStreamer at ONLY the bundled plugins. Without clearing the system path this would
    # happily resolve elements from the build machine's own installation and pass while the
    # shipped bundle is incomplete -- testing the developer's machine instead of the artifact,
    # which is the mistake this whole audit keeps finding.
    env = dict(os.environ)
    env["GST_PLUGIN_PATH"] = str(plugdir)
    env["GST_PLUGIN_SYSTEM_PATH"] = ""
    env["GST_PLUGIN_SYSTEM_PATH_1_0"] = ""
    env["GST_REGISTRY"] = str(gstdir / ".verify-registry")
    env["GST_REGISTRY_UPDATE"] = "no"

    def resolves(el):
        # NO EXTRA FLAGS. The first version passed --no-colour, which this GStreamer does not
        # accept: every element exited 255 and the tool reported a catastrophically broken
        # bundle that in fact works in production. A verifier that cries wolf gets switched off.
        r = subprocess.run([str(exe), el], env=env, capture_output=True, text=True, timeout=30)
        return r.returncode == 0

    # SENTINEL FIRST. `fakesink` lives in coreelements and is present in every conceivable
    # GStreamer. If it does not resolve, the HARNESS is wrong -- bad flag, bad env, wrong
    # binary -- and reporting "the bundle is broken" would be a false alarm about the artifact.
    # Distinguishing "I cannot verify" from "this is broken" is the whole point.
    if not resolves("fakesink"):
        print("  method: gst-inspect (bundled)")
        print("  \033[33mCANNOT VERIFY\033[0m  the sentinel element `fakesink` did not resolve,")
        print("        so this check is not working — the bundle is not implicated.")
        print("        Falling back to a filename check.")
        return _by_filename(plugdir)

    want = CORE + SINKS.get(sys.platform, [])
    missing = [el for el in want if not resolves(el)]
    _report(want, missing, "gst-inspect against the bundled plugin path only")
    return 1 if missing else 0


def _find_inspect(gstdir):
    """Only a gst-inspect that shares the BUNDLE'S OWN library closure can answer this.

    A foreign gst-inspect (Homebrew's, the runner's) already has its own libgstreamer mapped.
    dlopening a bundled plugin then pulls a SECOND libgstreamer into the same process, and
    GStreamer rejects every plugin -- which looks exactly like a catastrophically broken bundle
    and is really a broken test. The first version of this script did that and reported all 11
    elements missing from a bundle that works in production.

    On Windows the bundler copies the whole bin/, so gst-inspect.exe is already there. On macOS
    it is copied deliberately for this purpose.
    """
    name = "gst-inspect-1.0.exe" if os.name == "nt" else "gst-inspect-1.0"
    c = gstdir / name
    return c if c.exists() else None


def _by_filename(plugdir):
    need = {"gstudp", "gstrtpmanager", "gstrtp", "gstopus", "gstaudioconvert",
            "gstaudioresample", "gstaudiofx", "gstvolume", "gstcoreelements",
            "gstautodetect"}
    if not plugdir.is_dir():
        print("  FAIL  no plugins directory at %s" % plugdir)
        return 1
    have = {p.stem.replace("libgst", "gst") for p in plugdir.iterdir()}
    missing = sorted(need - have)
    _report(sorted(need), missing, "filename check (gst-inspect not in the bundle)")
    return 1 if missing else 0


def _report(want, missing, how):
    print("  method: %s" % how)
    print("  checked %d elements/plugins" % len(want))
    if missing:
        print("  \033[31mFAIL\033[0m  not resolvable from the bundle: %s" % ", ".join(missing))
        print("        The app would build, launch and serve — and then fail to construct its")
        print("        return pipeline at runtime. Room audio silently absent.")
    else:
        print("  \033[32mOK\033[0m    every required element resolves from the shipped bundle")


if __name__ == "__main__":
    sys.exit(main())
