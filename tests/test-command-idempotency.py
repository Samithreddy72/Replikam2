#!/usr/bin/env python3
"""A retried command must not become two executions.

Nothing prevented duplicate commands. A POST that timed out in the client, a double-clicked
button, or a proxy retry created a SECOND row and the agent executed both: two reboots, the
same script deployed twice, a golden-restore over a golden-restore. The confirmation gate does
not help, because a retry carries confirm=true as faithfully as the original.

And the broadcast endpoint checked ALLOWED_COMMANDS but NOT CONFIRM_REQUIRED -- so the
single-device path refused an unconfirmed reboot while the path that reboots EVERY DEVICE IN
THE ORG accepted it. The stricter gate was on the smaller blast radius.

This drives the real FastAPI app against a scratch database. It does not grep source.
"""
import os, pathlib, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BE = ROOT / "control-plane/backend"
TMP = tempfile.mkdtemp(prefix="nb-idem-")
os.environ["PAYLOAD_DIR"] = os.path.join(TMP, "payloads")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db")
os.environ.pop("NB_API_DOCS", None)
sys.path.insert(0, str(BE))

P = F = 0


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

print("NetBridge command idempotency")
print("=============================\n")

client = TestClient(M.app)


# --- stand in for an authenticated admin -------------------------------------------------
class _Actor:
    org = "org-test"
    id = "admin-test"
    email = "admin@test"
    role = "admin"


M.app.dependency_overrides[M.auth.require_admin] = lambda: _Actor()

db = SessionLocal()
for i in (1, 2):
    d = Device(id="dev%d" % i, org_id="org-test", name="bridge-%d" % i,
               pairing_code="BRIDGE-%04d" % (1000 + i))
    db.add(d)
db.commit()

BASE = "/admin/devices/dev1/commands"


def count(dtype, device="dev1"):
    s = SessionLocal()
    n = len(s.scalars(select(Command).where(Command.device_id == device,
                                            Command.type == dtype)).all())
    s.close()
    return n


print("  ---- a retried destructive command must not queue twice ----")
r1 = client.post(BASE, json={"type": "reboot", "confirm": True})
r2 = client.post(BASE, json={"type": "reboot", "confirm": True})
if r1.status_code == 200 and r2.status_code == 200:
    ok("both requests were accepted (a retry must not error)")
    if r1.json()["id"] == r2.json()["id"]:
        ok("the retry returned the SAME command id — one reboot, not two")
    else:
        no("the retry created a second command", "ids %s and %s" % (r1.json()["id"], r2.json()["id"]))
    if r2.json().get("deduplicated"):
        ok("the response says it was deduplicated (%s)" % r2.json()["deduplicated"])
    else:
        no("a deduplicated response should say so, or the caller cannot tell")
else:
    no("requests failed", "%s / %s" % (r1.status_code, r2.status_code))

if count("reboot") == 1:
    ok("exactly one reboot row exists in the database")
else:
    no("%d reboot rows exist — the device would execute each" % count("reboot"))

print("\n  ---- an explicit idempotency key works across states ----")
k = {"type": "deploy-script", "confirm": True, "idempotency_key": "abc-123"}
a = client.post(BASE, json=k)
b = client.post(BASE, json=k)
if a.status_code == 200 and a.json()["id"] == b.json()["id"]:
    ok("same idempotency_key returns the same command")
else:
    no("idempotency_key did not deduplicate")

print("\n  ---- read-only commands are NOT deduplicated ----")
# Asking for diagnostics twice must give a fresh answer, not a stale row.
c1 = client.post(BASE, json={"type": "diagnose"})
c2 = client.post(BASE, json={"type": "diagnose"})
if c1.status_code == 200 and c2.status_code == 200 and c1.json()["id"] != c2.json()["id"]:
    ok("two `diagnose` requests produce two commands (fresh answers, by design)")
else:
    no("read-only commands should not be deduplicated",
       "an operator refreshing diagnostics would get a stale row")

print("\n  ---- once a command reaches a terminal state, a new one may be queued ----")
s = SessionLocal()
row = s.scalars(select(Command).where(Command.type == "reboot")).first()
row.status = "done"
s.commit()
s.close()
r3 = client.post(BASE, json={"type": "reboot", "confirm": True})
if r3.status_code == 200 and r3.json()["id"] != r1.json()["id"]:
    ok("a genuinely new reboot is allowed once the previous one finished")
else:
    no("the in-flight guard must not block forever",
       "it should only suppress commands that are still pending or sent")

print("\n  ---- the confirm gate must cover BROADCAST too ----")
bad = client.post("/admin/commands/broadcast", json={"type": "reboot"})
if bad.status_code == 400:
    ok("unconfirmed fleet-wide reboot is REJECTED (400)")
else:
    no("broadcast accepted an unconfirmed reboot (%s)" % bad.status_code,
       "the single-device path refuses this; the org-wide path must not be laxer")

good = client.post("/admin/commands/broadcast", json={"type": "reboot", "confirm": True})
if good.status_code == 200:
    ok("a confirmed broadcast is accepted")
    queued = good.json().get("queued", [])
    if queued:
        ok("broadcast queued %d device(s)" % len(queued))
    s = SessionLocal()
    rows = s.scalars(select(Command).where(Command.type == "reboot",
                                           Command.device_id == "dev2")).all()
    s.close()
    if rows and all(r.timeout_s for r in rows):
        ok("broadcast commands carry a per-class timeout (they used to carry none)")
    else:
        no("broadcast commands have no timeout_s",
           "they would never be swept and could sit in `sent` forever")
else:
    no("confirmed broadcast was rejected (%s)" % good.status_code)

print("\n  ---- the single-device confirm gate still holds ----")
u = client.post(BASE, json={"type": "update"})
if u.status_code == 400:
    ok("unconfirmed update is still rejected")
else:
    no("confirm gate regressed (%s)" % u.status_code)

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
