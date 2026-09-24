#!/usr/bin/env python3
"""Can an unauthorised host on the LAN change anything on the bridge?

WHY THIS EXISTS
---------------
Until 2026-08-25 the answer was yes, and the consequence was not subtle:

    POST /api/set-peer {"ip": "<anything>", "port": 5004}

repoints the bridge's return audio, and the return stream is the meeting room's microphone.
The endpoint required no credential and reached every host on the venue LAN, including a guest
network. Nothing was written to any log an operator would look at. For a product whose purpose
is to sit in other organisations' meeting rooms, that was the finding that blocked release.

The fix is a trust boundary, not a new credential system: the presenter app does not talk to
this port over the LAN at all - it goes through the mesh helper's local proxy and arrives from
a tailnet address. Reads stay open because the fleet and the diagnostic tools depend on them
and they disclose no secret. Writes require loopback or the tailnet.

  python3 tests/test-bridge-web-auth.py
"""
import importlib.util, pathlib, sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-web.py"
spec = importlib.util.spec_from_file_location("bw", SRC)
bw = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(bw)
except Exception as e:
    print("  FAIL  bridge-web.py does not import: %s" % e); sys.exit(1)

text = SRC.read_text()
passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

print("\nbridge-web — who may mutate")
print("===========================")

print("\n  ---- the 2026-08-25 attack ----")
# A device on the venue LAN. This is the exact case that could steal room audio.
for lan in ("192.168.1.50", "10.0.0.7", "172.16.4.9", "8.8.8.8"):
    if not bw._mesh_or_local(lan):
        ok("LAN/public %-14s refused" % lan)
    else:
        no("%s would still be allowed to repoint the room's audio" % lan)

print("\n  ---- legitimate callers still work ----")
for good in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "100.64.0.1", "100.118.247.65", "100.127.255.254"):
    if bw._mesh_or_local(good):
        ok("allowed: %s" % good)
    else:
        no("%s is a legitimate caller and was refused" % good)

print("\n  ---- the tailnet range is checked properly ----")
# 100.64.0.0/10 is 100.64.x-100.127.x. A naive startswith("100.") also accepts ordinary
# public space such as 100.1.2.3, which is not the tailnet and would reopen the hole.
for imposter in ("100.1.2.3", "100.63.255.255", "100.128.0.1", "100.200.5.5"):
    if not bw._mesh_or_local(imposter):
        ok("not-tailnet %-16s refused" % imposter)
    else:
        no("%s is NOT in 100.64.0.0/10 but was allowed" % imposter)

print("\n  ---- parsed, not prefix-matched (2026-09-25 audit) ----")
# A text-prefix check took all of these for loopback or tailnet.
for spoof in ("::1:2:3:4", "::127.0.0.1", "::100.64.0.1", "::1abc", "127.evil", "100.64.0.1.nip.io"):
    if not bw._mesh_or_local(spoof):
        ok("refused: %r" % spoof)
    else:
        no("%r passed as the bridge itself or the mesh" % spoof)
for local in ("127.0.0.1", "127.9.9.9", "::1", "::ffff:127.0.0.1"):
    if bw._is_local(local):
        ok("loopback: %s" % local)
    else:
        no("%s is loopback and was not recognised" % local)
if not bw._is_local("100.64.0.1") and not bw._is_local("::1:2:3:4"):
    ok("a mesh address and a look-alike are not 'the bridge itself'")
else:
    no("_is_local accepts a non-loopback address")

print("\n  ---- malformed input cannot slip through ----")
for junk in ("", None, "not-an-ip", "100.", "100", "100.abc.1.1", "::ffff:192.168.1.9"):
    if not bw._mesh_or_local(junk):
        ok("refused: %r" % junk)
    else:
        no("malformed source accepted", junk)

print("\n  ---- the check guards ALL mutations, not just set-peer ----")
# Placing it per-endpoint would mean the next mutating endpoint is protected only if someone
# remembers. It sits at the top of do_POST instead.
i_post = text.find("def do_POST")
i_check = text.find("_mesh_or_local(peer)")
i_first = min(x for x in (text.find('if path == "/api/set-peer"', i_post),
                          text.find('"/api/return-tune"', i_post),
                          text.find('"/api/unlock"', i_post)) if x > 0)
if 0 < i_check < i_first:
    ok("the check runs before ANY endpoint is dispatched")
else:
    no("mutations are dispatched before the source is checked", (i_check, i_first))
if "def do_GET" in text and "_mesh_or_local" not in text[text.find("def do_GET"):text.find("def do_POST")]:
    ok("reads are NOT restricted — fleet diagnostics keep working")
else:
    no("read-only endpoints were restricted too; that breaks diagnostics for no benefit")

print("\n  ---- a refusal is a real 403, and is logged ----")
if "status=403" in text:
    ok("refusal returns 403, not a 200 carrying an error object")
else:
    no("a client cannot distinguish 'denied' from 'worked'")
if "_audit(path, peer, False)" in text and "_audit(path, peer, True)" in text:
    ok("both allowed and refused mutations are journalled with their source")
else:
    no("mutations are not audit-logged — a redirection would leave no trace")

print("\n  ---- no shell in the network-facing process ----")
body = text[text.find("def sh(cmd):"):text.find("def _return_pcm")]
if "shell=True" not in body.replace("NOT shell=True", ""):
    ok("sh() no longer uses a shell")
else:
    no("shell=True is still live in the process that takes network input")
if 'if any(c in cmd for c in ("|", ">", "<", "&&", ";", "$(", "`")):' in text:
    ok("a command needing a shell is refused loudly, not silently mis-run")
else:
    no("a future caller could pass a pipe and get an empty result with no explanation")

print("\n  ---- negative control ----")
if bw._mesh_or_local("127.0.0.1") and not bw._mesh_or_local("192.168.1.50"):
    ok("the function distinguishes allowed from denied — the tests mean something")
else:
    no("cannot tell the two apart; these tests prove nothing")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
