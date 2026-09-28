#!/usr/bin/env python3
"""Which OS version does a bridge report after an OS update? (2026-09-28)

/etc/bridge is bind-mounted from /data, which BOTH A/B slots share. Its version file is written
once, at flash, so after 2.2.0 -> 2.2.1 the bridge kept reporting 2.2.0: the panel, `nb ota` and
the rollout's "already on this version" check were all wrong, and a second rollout of 2.2.1
re-targeted bridges already on it. The running slot's own stamp (/etc/netbridge-image-version,
written at flash and by bridge-update.sh for the slot it installs) is the truth.

Runs the REAL bridge-web.py gather() over a fake filesystem, and the REAL bridge-identity.sh in a
sandbox (its /etc paths moved into a temp dir, hostname tools stubbed).

  python3 tests/test-bridge-version.py
"""
import importlib.util, json, os, pathlib, re, shutil, subprocess, sys, tempfile, types

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bw", REPO / "pi" / "scripts" / "bridge-web.py")
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:300]) if detail else ""))

# ---- bridge-web: what /api/status (and so telemetry) says ---------------------------------------
bw.sh = lambda cmd: ""                                  # no programs: this is about files
real_run = subprocess.run                               # (bw.subprocess IS this module)
bw.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
FS = {}
real_read = bw.read
bw.read = lambda p: FS[p] if p in FS else ("" if p.startswith("/etc/") else real_read(p))

def status(files):
    FS.clear(); FS.update(files)
    getattr(bw, "_CACHE", {}).clear()
    if hasattr(bw, "_status_cache"):
        bw._status_cache[0], bw._status_cache[1] = 0.0, None
    bw.time.monotonic = lambda c=[0.0]: (c.__setitem__(0, c[0] + 100.0), c[0])[1]
    return bw.gather()

REL_OLD = json.dumps({"version": "2.2.0-aaaaaaa", "git_sha": "a" * 40})
REL_NEW = json.dumps({"version": "2.2.1-bbbbbbb", "git_sha": "b" * 40})
d = status({"/etc/bridge/version": "2.2.0-aaaaaaa", "/etc/bridge/release.json": REL_OLD,
            "/etc/netbridge-image-version": "2.2.1-bbbbbbb", "/etc/netbridge-release.json": REL_NEW})
check(d.get("version") == "2.2.1-bbbbbbb", "after an OS update to 2.2.1 the bridge reports 2.2.1, not the flashed 2.2.0",
      d.get("version"))
check((d.get("build") or {}).get("git_sha") == "b" * 40, "the build record is the running slot's, not the flashed one",
      d.get("build"))
d = status({"/etc/bridge/version": "2.2.1-bbbbbbb", "/etc/netbridge-image-version": "2.2.0-aaaaaaa"})
check(d.get("version") == "2.2.0-aaaaaaa", "after a rollback to 2.2.0 it reports 2.2.0", d.get("version"))
d = status({"/etc/bridge/version": "2.1.0-ccccccc"})
check(d.get("version") == "2.1.0-ccccccc" and (d.get("build") or {}).get("version") == "2.1.0-ccccccc",
      "an image without the per-slot files still reports /etc/bridge/version", d.get("version"))
d = status({})
check(d.get("version") == "dev", "nothing at all: 'dev', never invented", d.get("version"))

# ---- the CI build writes the record where a running bridge can read it ---------------------------
ci = (REPO / "factory" / "ci-build-image.sh").read_text()
code = "\n".join(l for l in ci.splitlines() if not l.lstrip().startswith("#"))
check(re.search(r"^cp /etc/bridge/release\.json /etc/netbridge-release\.json$", code, re.M) is not None,
      "ci-build-image.sh also writes /etc/netbridge-release.json (outside the /data bind)")

# ---- bridge-identity.sh keeps /etc/bridge/version in step for every other reader -----------------
T = pathlib.Path(tempfile.mkdtemp())
(T / "bin").mkdir(); (T / "etc-bridge").mkdir()
src = (REPO / "pi" / "scripts" / "bridge-identity.sh").read_text()
sand = (src.replace("/etc/netbridge-image-version", str(T / "slot-version"))
           .replace("/etc/bridge/", str(T / "etc-bridge") + "/")
           .replace("/proc/cpuinfo", str(T / "cpuinfo"))
           .replace("/etc/hostname", str(T / "hostname-file"))
           .replace("/etc/hosts", str(T / "hosts")))
code = "\n".join(l for l in sand.splitlines() if not l.lstrip().startswith("#"))
if re.search(r"/etc/|/proc/", code):
    check(False, "the sandbox copy of bridge-identity.sh still names a device path - not running it")
    print("\n  %d passed, %d failed" % (passed, failed)); sys.exit(1)
(T / "identity.sh").write_text(sand)
(T / "etc-bridge" / "pairing-code").write_text("TEST\n")
for name, body in (("hostname", "echo netbridge-TEST"), ("hostnamectl", "exit 0"), ("logger", "exit 0")):
    (T / "bin" / name).write_text("#!/bin/bash\n%s\n" % body); os.chmod(T / "bin" / name, 0o755)
env = dict(os.environ, PATH="%s:%s" % (T / "bin", os.environ["PATH"]))
env.pop("BRIDGE_VERSION", None)
def identity(slot, seed):
    for p, v in (("slot-version", slot), ("etc-bridge/version", seed)):
        f = T / p
        f.unlink() if f.exists() else None
        if v is not None:
            f.write_text(v + "\n")
    real_run(["bash", str(T / "identity.sh")], env=env, capture_output=True, timeout=30)
    f = T / "etc-bridge" / "version"
    return f.read_text().strip() if f.exists() else None

check(identity("2.2.1-bbbbbbb", "2.2.0-aaaaaaa") == "2.2.1-bbbbbbb",
      "every boot: /etc/bridge/version follows the running slot (2.2.0 seed -> 2.2.1)")
check(identity("2.2.0-aaaaaaa", "2.2.1-bbbbbbb") == "2.2.0-aaaaaaa", "...and back after a rollback")
check(identity(None, "2.1.0-ccccccc") == "2.1.0-ccccccc", "no per-slot stamp: the seed is left alone")
check(identity(None, None) == "dev", "no stamp and no seed: 'dev'")

shutil.rmtree(T, ignore_errors=True)
print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
