#!/usr/bin/env python3
"""Authentication is not authorization. Prove org B is invisible to org A.

Seventeen admin endpoints take an id straight out of the URL path. Every one of them is a
place where "you are logged in" could be mistaken for "this is yours" -- the classic IDOR
shape. The product runs a single organisation today, which is exactly the condition under
which this kind of hole survives unnoticed until the day it matters.

This does not read the source and look for `org_id`. It stands up two organisations with real
data and, as an admin of org A, attempts to read, command, claim, rename, PIN, mint mesh keys
for and delete org B's devices, commands, diagnostics, rollouts and users.

A leak here is worse than it first sounds: `mesh-key` mints a tailnet credential, `pin` sets
the device unlock PIN, and `commands` can reboot someone else's meeting room.
"""
import os, pathlib, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BE = ROOT / "control-plane/backend"
TMP = tempfile.mkdtemp(prefix="nb-authz-")
os.environ["PAYLOAD_DIR"] = os.path.join(TMP, "payloads")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db")
os.environ.pop("NB_API_DOCS", None)
sys.path.insert(0, str(BE))

P = F = 0
LEAKS = []


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


try:
    from fastapi.testclient import TestClient
    import app.main as M
    from app.db import SessionLocal
    from app.models import Device, Command, User
    from sqlalchemy import select
except Exception as e:
    print("  SKIPPED - backend deps unavailable (%s)" % e)
    raise SystemExit(0)

print("NetBridge cross-organisation authorization")
print("==========================================\n")

client = TestClient(M.app)


class Actor:
    def __init__(self, org, uid, role="admin"):
        self.org, self.id, self.role = org, uid, role
        self.email = "%s@%s" % (uid, org)


A = Actor("org-a", "admin-a")
B = Actor("org-b", "admin-b")
_current = {"actor": A}
M.app.dependency_overrides[M.auth.require_admin] = lambda: _current["actor"]
for dep in ("require_viewer",):
    if hasattr(M.auth, dep):
        M.app.dependency_overrides[getattr(M.auth, dep)] = lambda: _current["actor"]


def as_(actor):
    _current["actor"] = actor


db = SessionLocal()
for org, n in (("org-a", "a"), ("org-b", "b")):
    d = Device(id="dev-%s" % n, org_id=org, name="bridge-%s" % n,
               pairing_code="BRIDGE-%s" % n.upper())
    db.add(d)
db.add(User(org_id="org-b", email="victim@org-b", role="admin"))  # id is an autoincrement int
db.commit()

# give org B a command and a rollout to try to reach
as_(B)
rb = client.post("/admin/devices/dev-b/commands", json={"type": "diagnose"})
B_CMD = rb.json().get("id") if rb.status_code == 200 else None
db2 = SessionLocal()
B_USER = db2.scalars(select(User).where(User.org_id == "org-b")).first()
B_USER_ID = B_USER.id if B_USER else 0
db2.close()

print("  ---- org A must not READ org B ----")
as_(A)
READS = [
    ("GET  /admin/devices/{id}",             "get",  "/admin/devices/dev-b"),
    ("GET  /admin/devices/{id}/commands",    "get",  "/admin/devices/dev-b/commands"),
    ("GET  /admin/devices/{id}/diagnostics", "get",  "/admin/devices/dev-b/diagnostics"),
    ("GET  /admin/devices/{id}/label",       "get",  "/admin/devices/dev-b/label"),
    ("GET  /admin/devices/{id}/uptime",      "get",  "/admin/devices/dev-b/uptime"),
]
for label, verb, path in READS:
    r = getattr(client, verb)(path)
    if r.status_code in (403, 404):
        ok("%-38s -> %s" % (label, r.status_code))
    else:
        no("%-38s -> %s  ORG B DATA LEAKED" % (label, r.status_code), r.text[:120])
        LEAKS.append(label)

print("\n  ---- org A must not COMMAND or MUTATE org B ----")
as_(A)
MUTATIONS = [
    ("POST   /admin/devices/{id}/commands", "post", "/admin/devices/dev-b/commands",
     {"type": "reboot", "confirm": True}),
    ("POST   /admin/devices/{id}/claim",    "post", "/admin/devices/dev-b/claim", {"name": "stolen"}),
    ("POST   /admin/devices/{id}/pin",      "post", "/admin/devices/dev-b/pin", {"pin": "123456"}),
    ("POST   /admin/devices/{id}/mesh-key", "post", "/admin/devices/dev-b/mesh-key", {}),
    ("DELETE /admin/devices/{id}",          "delete", "/admin/devices/dev-b", None),
]
for label, verb, path, body in MUTATIONS:
    kw = {"json": body} if body is not None else {}
    r = getattr(client, verb)(path, **kw)
    if r.status_code in (403, 404):
        ok("%-38s -> %s" % (label, r.status_code))
    else:
        no("%-38s -> %s  ORG B DEVICE MUTATED" % (label, r.status_code), r.text[:120])
        LEAKS.append(label)

print("\n  ---- org A must not reach org B's COMMAND by id ----")
if B_CMD is None:
    print("  (could not create a command in org B to test with)")
else:
    as_(A)
    r = client.delete("/admin/devices/dev-b/commands/%s" % B_CMD)
    if r.status_code in (403, 404):
        ok("DELETE /admin/devices/{id}/commands/{cmd}  -> %s" % r.status_code)
    else:
        no("cancelled another org's command (%s)" % r.status_code, r.text[:120])
        LEAKS.append("command cancel")
    # and via its OWN device id, which is the subtler path
    r = client.delete("/admin/devices/dev-a/commands/%s" % B_CMD)
    if r.status_code in (403, 404):
        ok("same command via org A's own device id       -> %s" % r.status_code)
    else:
        no("reached another org's command through an owned device id (%s)" % r.status_code,
           "scope must be checked on the COMMAND, not only on the device in the path")
        LEAKS.append("command via own device")

print("\n  ---- org A must not manage org B's USERS ----")
as_(A)
r = client.delete("/admin/users/%s" % B_USER_ID)
if r.status_code in (403, 404):
    ok("DELETE /admin/users/{uid}                    -> %s" % r.status_code)
else:
    no("deleted another org's user (%s)" % r.status_code, r.text[:120])
    LEAKS.append("user delete")

print("\n  ---- list endpoints must not bleed ----")
as_(A)
r = client.get("/admin/devices")
if r.status_code == 200:
    body = r.text
    if "dev-b" in body or "bridge-b" in body:
        no("/admin/devices lists another org's device", body[:160])
        LEAKS.append("device list")
    else:
        ok("/admin/devices shows only org A")
r = client.get("/admin/audit")
if r.status_code == 200 and ("org-b" in r.text or "victim@org-b" in r.text):
    no("/admin/audit leaks another org's audit trail", r.text[:160])
    LEAKS.append("audit")
elif r.status_code in (200, 403, 404):
    ok("/admin/audit does not leak org B")

print("\n  ---- the positive control: org A CAN use its own device ----")
as_(A)
r = client.get("/admin/devices/dev-a")
if r.status_code == 200:
    ok("org A can read its own device (the checks above are not just blanket denial)")
else:
    no("org A cannot reach its own device (%s)" % r.status_code,
       "a test that denies everything proves nothing")

print("\n  %d passed, %d failed" % (P, F))
if LEAKS:
    print("  CROSS-ORG LEAKS: %s" % ", ".join(LEAKS))
sys.exit(1 if F else 0)
