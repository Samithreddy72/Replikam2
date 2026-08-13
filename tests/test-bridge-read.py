#!/usr/bin/env python3
"""Can the remote reader be talked into reading something it should not?

WHY THIS EXISTS
---------------
This is the one component whose bug is a leak rather than an outage. It exposes a read of the
bridge's filesystem to anyone holding a fleet admin token, so every refusal it makes is a
security boundary and belongs in a test rather than in a comment.

Two real bugs were caught here before it ran anywhere:

  1. ROOTS were compared UNRESOLVED. A path resolves through symlinks before the check, so a
     legitimate file under a root that is itself a symlink resolved to something that did not
     match the root string and was refused. /home/pi/flight.txt on this bridge is exactly
     that case.

  2. "BOOTSTRAP_TOKEN: xyz" was NOT redacted. The pattern used \\btoken\\b, and "_" is a word
     character, so there is no boundary before TOKEN in BOOTSTRAP_TOKEN. That variable holds
     the fleet enrolment secret in /etc/default/bridge-agent — the single worst thing in the
     readable roots.

  python3 tests/test-bridge-read.py
"""
import importlib.util, os, pathlib, shutil, sys, tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-read.py"

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))


def fresh(roots=None):
    spec = importlib.util.spec_from_file_location("br_%d" % id(roots), SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    if roots:
        m.ROOTS = tuple(os.path.realpath(r) for r in roots)
    return m


def allowed(mod, path):
    """True if resolve() lets it through."""
    try:
        mod.resolve(path)
        return True
    except SystemExit:
        return False


print("\nRemote file reader")
print("==================")

T = os.path.realpath(tempfile.mkdtemp(prefix="brtest-"))
os.makedirs(T + "/data/config", exist_ok=True)
os.makedirs(T + "/outside", exist_ok=True)
open(T + "/data/ok.txt", "w").write("hello\n")
open(T + "/outside/secret.txt", "w").write("nope\n")
open(T + "/data/key.pem", "w").write("-----BEGIN PRIVATE KEY-----\n")
open(T + "/data/ssh_host_rsa_key", "w").write("private\n")
os.symlink(T + "/outside/secret.txt", T + "/data/escape")
br = fresh([T + "/data"])

print("\n  ---- what it must allow ----")
for label, p in (("a plain file in a root", T + "/data/ok.txt"),
                 ("the root directory itself", T + "/data")):
    ok(label) if allowed(br, p) else no("refused something legitimate: %s" % label)

print("\n  ---- what it must refuse ----")
cases = [
    ("a path outside every root",        T + "/outside/secret.txt"),
    ("a symlink escaping the root",      T + "/data/escape"),
    ("traversal with ..",                T + "/data/../outside/secret.txt"),
    ("a relative path",                  "data/ok.txt"),
    ("a .pem key inside a root",         T + "/data/key.pem"),
    ("an ssh host private key",          T + "/data/ssh_host_rsa_key"),
]
for label, p in cases:
    no("ALLOWED something it must refuse: %s" % label, p) if allowed(br, p) else ok(label)

print("\n  ---- the named credential files ----")
br2 = fresh(["/etc/bridge", "/etc/default", "/data"])
for label, p in (("the device's fleet token", "/etc/bridge/agent.token"),
                 ("the Wi-Fi setup password", "/etc/bridge/setup-wifi-pass")):
    no("ALLOWED %s" % label, p) if allowed(br2, p) else ok(label)

print("\n  ---- redaction: a value that looks like a secret never comes back ----")
samples = [
    ("BOOTSTRAP_TOKEN: abc123",          "abc123"),
    ("CONTROL_TOKEN=xyz789",             "xyz789"),
    ("  api_secret = hunter2",           "hunter2"),
    ("TS_AUTHKEY=tskey-abcdef",          "tskey-abcdef"),
    ("password: correcthorse",           "correcthorse"),
    ("MESH_KEY=deadbeef",                "deadbeef"),
]
for line, secret in samples:
    out, n = br.redact(line + "\n")
    if secret in out:
        no("leaked a secret value from: %s" % line, out.strip())
    elif n != 1:
        no("did not count the redaction: %s" % line, out.strip())
    else:
        ok("redacted %-28s -> %s" % (line.strip()[:28], out.strip()))

print("\n  ---- redaction must not mangle ordinary lines ----")
for line in ("hw_ptr : 93024", "state: RUNNING", "NET_AUDIO_LATENCY=300",
             "c_srate = 48000,44100,32000"):
    out, n = br.redact(line + "\n")
    if n == 0 and out.strip() == line.strip():
        ok("left alone: %s" % line)
    else:
        no("mangled an ordinary line: %s" % line, out.strip())

print("\n  ---- the two bugs this test was written for ----")
# 1. a root that is itself a symlink (exactly /home/pi/flight.txt -> /data on the bridge)
L = os.path.realpath(tempfile.mkdtemp(prefix="brlink-"))
os.makedirs(L + "/real", exist_ok=True)
open(L + "/real/f.txt", "w").write("x\n")
os.symlink(L + "/real", L + "/link")
br3 = fresh([L + "/link"])
ok("a file under a symlinked ROOT is still readable") if allowed(br3, L + "/link/f.txt") \
    else no("a symlinked root refuses its own files (the resolved-roots bug)")

# 2. the prefixed-name redaction bug
out, n = br.redact("BOOTSTRAP_TOKEN: supersecret\n")
ok("BOOTSTRAP_TOKEN is redacted despite the prefix") if "supersecret" not in out \
    else no("BOOTSTRAP_TOKEN leaked — \\btoken\\b does not match inside an underscored name")

print("\n  ---- negative control ----")
out, n = br.redact("nothing sensitive here\n")
ok("a clean line is reported as 0 redactions") if n == 0 else no("counts redactions that did not happen")

for d in (T, L):
    shutil.rmtree(d, ignore_errors=True)
print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
