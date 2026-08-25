#!/usr/bin/env python3
"""A verdict the control plane has reached must not be rewritten by a late device report.

`command_result` did `c.status = body.status` unconditionally. The dangerous direction is not
the obvious one: a command an operator CANCELLED, or one that EXPIRED because its deadline
passed, could be turned into `done` minutes later by an agent that finally got around to
answering. The panel would then show a green tick for a command the operator believes they
stopped — and the record that they stopped it would be gone.

A device finishing after the deadline is a real event worth knowing about. It is simply not the
same event as "this succeeded", and conflating them destroys the evidence needed to reconstruct
what happened. So: terminal states are final, and the late report is appended alongside.

Drives the real FastAPI app against a scratch database.
"""
import os, pathlib, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BE = ROOT / "control-plane/backend"
TMP = tempfile.mkdtemp(prefix="nb-fsm-")
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
    from app.models import Device, Command
    from sqlalchemy import select
except Exception as e:
    print("  SKIPPED - backend deps unavailable (%s)" % e)
    raise SystemExit(0)

print("NetBridge command state machine")
print("===============================\n")

client = TestClient(M.app)


class Actor:
    org, id, email, role = "org-fsm", "admin", "a@b", "admin"


DEV = "dev-fsm"
M.app.dependency_overrides[M.auth.require_admin] = lambda: Actor()

db = SessionLocal()
db.add(Device(id=DEV, org_id="org-fsm", name="fsm", pairing_code="BRIDGE-FSM"))
db.commit()
db.close()


class _Dev:
    id = DEV


M.app.dependency_overrides[M.auth.require_device] = lambda: _Dev()


def make(status):
    s = SessionLocal()
    c = Command(device_id=DEV, type="diagnose", args={}, status=status, timeout_s=60)
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def status_of(cid):
    s = SessionLocal()
    c = s.get(Command, cid)
    out = (c.status, c.output or "")
    s.close()
    return out


print("  ---- a result IS accepted from a non-terminal state ----")
for start in ("pending", "sent"):
    cid = make(start)
    r = client.post("/v1/commands/%d/result" % cid, json={"status": "done", "output": "ok"})
    st, out = status_of(cid)
    if r.status_code == 200 and st == "done":
        ok("%-8s -> done accepted" % start)
    else:
        no("%s should accept a result (got %s, status=%s)" % (start, r.status_code, st))

print("\n  ---- terminal states must NOT be rewritten ----")
cases = [
    ("cancelled", "done",   "an operator cancelled it; a late 'done' must not undo that"),
    ("expired",   "done",   "the deadline passed; finishing later is not the same as succeeding"),
    ("done",      "failed", "a completed command must not be retroactively failed"),
    ("failed",    "done",   "a failure must not be quietly upgraded"),
    ("rejected",  "done",   "a refusal stands"),
]
for start, attempt, why in cases:
    cid = make(start)
    r = client.post("/v1/commands/%d/result" % cid,
                    json={"status": attempt, "output": "late device output"})
    st, out = status_of(cid)
    if st == start:
        ok("%-9s survives a late '%s'  (%s)" % (start, attempt, why))
    else:
        no("%s was overwritten to %s" % (start, st), why)

print("\n  ---- but the late report must be PRESERVED, not discarded ----")
cid = make("expired")
client.post("/v1/commands/%d/result" % cid,
            json={"status": "done", "output": "finished at last"})
st, out = status_of(cid)
if "late report" in out and "finished at last" in out:
    ok("the device's late output is appended as evidence")
else:
    no("the late report was thrown away", "an operator cannot reconstruct what happened")
if "that verdict stands" in out:
    ok("the appended note says explicitly which verdict stands")
else:
    no("the note should state that the original verdict is authoritative")
if st == "expired":
    ok("and the status is still `expired` (both facts survive)")
else:
    no("status changed to %s" % st)

print("\n  ---- the response tells the caller what happened ----")
cid = make("cancelled")
r = client.post("/v1/commands/%d/result" % cid, json={"status": "done", "output": "x"})
j = r.json() if r.status_code == 200 else {}
if j.get("recorded") == "late-report":
    ok("the device is told its report was recorded as a late report")
else:
    no("a silent 200 would let the agent believe it set the status", str(j)[:120])
if j.get("status") == "cancelled":
    ok("and is told the authoritative status")
else:
    no("the response should carry the surviving status")

print("\n  ---- a device may not report on another device's command ----")
cid = make("pending")
s = SessionLocal()
s.add(Device(id="other-dev", org_id="org-fsm", name="other", pairing_code="BRIDGE-OTH"))
s.commit()
s.close()
M.app.dependency_overrides[M.auth.require_device] = lambda: type("D", (), {"id": "other-dev"})()
r = client.post("/v1/commands/%d/result" % cid, json={"status": "done", "output": "hijack"})
st, _ = status_of(cid)
if r.status_code == 404 and st == "pending":
    ok("a foreign device gets 404 and cannot set the status")
else:
    no("another device altered this command (%s, status=%s)" % (r.status_code, st))

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
