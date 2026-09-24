#!/usr/bin/env python3
"""Fleet payload rules (2026-09-24): the fleet must accept every file a bridge's catalog lets
it install — not only *.sh — and still refuse junk. Runs the REAL name/kind rules extracted
from control-plane/backend/app/main.py (no FastAPI needed), checked against the REAL catalog."""
import pathlib, re, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = (REPO / "control-plane/backend/app/main.py").read_text()
CATALOG = (REPO / "pi/configs/updatable.conf").read_text()
passed = failed = 0
def check(cond, msg):
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(("  PASS  " if cond else "  FAIL  ") + msg)

ns = {"re": re}
m = re.search(r"^_PAYLOAD_NAME = re\.compile\(.*\)$", SRC, re.M)
k = re.search(r"^def _payload_kind\(name: str\) -> str:\n(?:    .*\n)+", SRC, re.M)
check(bool(m and k), "payload name + kind rules found in the fleet backend")
if m and k:
    exec(m.group(0) + "\n" + k.group(0), ns)
    names = [l.split()[0] for l in CATALOG.splitlines() if l.strip() and not l.lstrip().startswith("#") and len(l.split()) >= 5]
    bad = [n for n in names if not ns["_PAYLOAD_NAME"].fullmatch(n)]
    check(not bad, "every catalog file can be uploaded (%d names)%s" % (len(names), "" if not bad else ": " + ", ".join(bad)))
    check(ns["_payload_kind"]("owner_ssh_authorized_keys") == "keys", "owner SSH keys are checked as keys")
    check(ns["_payload_kind"]("dropin.bridge-web") == "dropin", "unit drop-ins are checked as drop-ins")
    check(ns["_payload_kind"]("bridge-web.py") == "script", "Python must start with #!")
    for junk in ("../etc/passwd", ".hidden", "a/b", "", "x" * 81, "-rf"):
        check(not ns["_PAYLOAD_NAME"].fullmatch(junk), "name %r refused" % junk[:12])
check('".." in name' in SRC and 'name.endswith(".sig")' in SRC, "'..' and '.sig' names refused")
check("_PAYLOAD_MAX = 2 * 1024 * 1024" in SRC, "uploads capped at 2 MB (OS images go through publish-ota)")
check('if n.endswith(".sh"))' not in SRC, "the payload list is no longer .sh-only")
check('@app.get("/admin/payloads/ota")' in SRC, "the fleet lists the OS versions it offers")
print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
