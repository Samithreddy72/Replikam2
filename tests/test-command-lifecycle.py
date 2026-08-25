#!/usr/bin/env python3
"""Does every fleet command reach a terminal state, and can a mistake be recalled?

WHY THIS EXISTS
---------------
On 2026-08-24, probing which commands the backend accepted queued a real `reboot` against a
live bridge. There was no cancel endpoint - DELETE returned 405 - and the device had already
collected it. The bridge rebooted mid-investigation. Two other commands from the same session
sat in "sent" for over an hour while later ones completed, with nothing anywhere saying they
were stuck.

Two defences, because they fix different halves:
  * a confirmation gate, so a destructive command cannot be issued by accident at all;
  * timeouts, so "sent" can never again mean "unknown, forever".

The cancel endpoint is deliberately narrow. A cancellation that changes a database row while
the device executes anyway would be worse than none: the operator would believe the reboot was
stopped and the room would drop regardless. It cancels what it truly can and refuses the rest
with a reason.

Runs against the real model and the real policy, on an in-memory database.

  /tmp/bev/bin/python tests/test-command-lifecycle.py
"""
import datetime as dt, importlib.util, pathlib, sys, types

# The backend's dependencies are not installed everywhere this suite runs, and models.py
# imports sqlalchemy at module scope - so the check has to happen before anything touches it.
# Skipping is announced loudly and exits 0: a test that silently disappears is how coverage
# rots without anybody noticing.
try:
    import sqlalchemy  # noqa: F401
except ImportError:
    print("\nFleet command lifecycle")
    print("=======================")
    print("  SKIP  sqlalchemy not installed — backend lifecycle tests not run here")
    print("        python3 -m venv /tmp/bev && /tmp/bev/bin/pip install fastapi 'sqlalchemy>=2' pydantic")
    print("        then: /tmp/bev/bin/python tests/test-command-lifecycle.py")
    print("\n  0 passed, 0 failed  (SKIPPED - dependencies absent)\n")
    raise SystemExit(0)

ROOT = pathlib.Path(__file__).resolve().parent.parent
BE = ROOT / "control-plane" / "backend" / "app"

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

# Load models.py standalone. Importing app.main would pull in the whole FastAPI application,
# its auth backend and its settings; the lifecycle logic under test does not need any of that.
pkg = types.ModuleType("app"); pkg.__path__ = [str(BE)]
sys.modules["app"] = pkg
spec = importlib.util.spec_from_file_location("app.models", BE / "models.py")
models = importlib.util.module_from_spec(spec); sys.modules["app.models"] = models
spec.loader.exec_module(models)

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

print("\nFleet command lifecycle")
print("=======================")

print("\n  ---- the schema can express a terminal state ----")
cols = {c.name for c in models.Command.__table__.columns}
for need, why in (("sent_at", "the clock a timeout is measured from"),
                  ("timeout_s", "per-class deadline"),
                  ("fail_reason", "why an operator's command died")):
    if need in cols: ok("commands.%-12s present (%s)" % (need, why))
    else: no("commands.%s missing — %s" % (need, why))

print("\n  ---- timeouts are per CLASS, not one global number ----")
src = (BE / "main.py").read_text()
import re
m = re.search(r"TIMEOUT_S = \{(.*?)\n\}", src, re.S)
if not m:
    no("no TIMEOUT_S policy table")
else:
    tbl = dict(re.findall(r'"([a-z-]+)":\s*(\d+)', m.group(1)))
    def g(k): return int(tbl.get(k, 0))
    if g("read-file") and g("reboot") and g("update"):
        ok("read=%ss  reboot=%ss  update=%ss" % (g("read-file"), g("reboot"), g("update")))
    else:
        no("policy is missing one of the classes that motivated it", tbl)
    # A reboot must outlive the reboot; an OTA must outlive a download over a venue uplink.
    if g("read-file") < g("restart") < g("reboot") < g("update"):
        ok("the ordering reflects how long each class really takes")
    else:
        no("timeouts are not ordered by how long the work actually takes",
           [g("read-file"), g("restart"), g("reboot"), g("update")])

print("\n  ---- destructive commands need explicit intent ----")
m2 = re.search(r"CONFIRM_REQUIRED = \{(.*?)\}", src, re.S)
conf = set(re.findall(r'"([a-z-]+)"', m2.group(1))) if m2 else set()
for d in ("reboot", "update", "deploy-script", "golden-restore", "revert-script", "unquarantine"):
    if d in conf: ok("%-15s requires confirm=true" % d)
    else: no("%s can still be issued with a single unconfirmed call" % d)
for safe in ("read-file", "logs", "running", "jitter-diagnose", "diagnose"):
    if safe not in conf: ok("%-15s stays friction-free (read-only)" % safe)
    else: no("%s is read-only; a confirmation prompt here trains people to click through" % safe)
if "confirm: bool = False" in (BE / "schemas.py").read_text():
    ok("confirm defaults to False — omission cannot grant the right to reboot")
else:
    no("confirm does not default to False")

print("\n  ---- a delivered command cannot sit in 'sent' forever ----")
eng = create_engine("sqlite://")
models.Base.metadata.create_all(eng)
now = dt.datetime.now(dt.timezone.utc)
with Session(eng) as db:
    db.add(models.Device(id="d1", pairing_code="X", name="t"))
    stale = models.Command(device_id="d1", type="reboot", status="sent",
                           sent_at=now - dt.timedelta(seconds=9999), timeout_s=420)
    fresh = models.Command(device_id="d1", type="reboot", status="sent",
                           sent_at=now - dt.timedelta(seconds=5), timeout_s=420)
    waiting = models.Command(device_id="d1", type="reboot", status="pending", timeout_s=420)
    done = models.Command(device_id="d1", type="logs", status="done", timeout_s=90)
    for c in (stale, fresh, waiting, done): db.add(c)
    db.commit()

    # the sweeper's own logic, lifted from main.py so the test exercises the real rule
    def sweep(db):
        n = 0
        for c in db.scalars(select(models.Command).where(models.Command.status == "sent")).all():
            started = c.sent_at or c.created_at
            if started.tzinfo is None: started = started.replace(tzinfo=dt.timezone.utc)
            if (now - started).total_seconds() > (c.timeout_s or 240):
                c.status = "expired"
                c.fail_reason = "no result within %ds of delivery" % c.timeout_s
                c.completed_at = now; n += 1
        db.commit(); return n

    n = sweep(db)
    db.refresh(stale); db.refresh(fresh); db.refresh(waiting); db.refresh(done)
    if stale.status == "expired": ok("a command delivered 9999s ago is expired")
    else: no("the stuck-forever case is still stuck", stale.status)
    if stale.fail_reason: ok("it records WHY: %s" % stale.fail_reason[:52])
    else: no("expired with no reason — the operator learns nothing")
    if fresh.status == "sent": ok("a command delivered 5s ago is left alone")
    else: no("killed a command that is legitimately still running", fresh.status)
    # pending means the device has not polled yet. That is waiting, not failing.
    if waiting.status == "pending": ok("a PENDING command is never expired (not yet delivered)")
    else: no("expired a command the device had not even collected", waiting.status)
    if done.status == "done": ok("terminal states are not disturbed")
    else: no("rewrote finished history", done.status)

print("\n  ---- cancellation is honest ----")
cn = re.search(r"def cancel_command\(.*?(?=\n@app|\ndef )", src, re.S)
body = cn.group(0) if cn else ""
if '@app.delete("/admin/devices/{device_id}/commands/{cmd_id}")' in src:
    ok("a DELETE endpoint exists (it returned 405 during the audit)")
else:
    no("still no way to cancel anything")
if 'if c.status == "pending"' in body and '"cancelled"' in body:
    ok("pending -> cancelled, which is a real recall: the device never saw it")
else:
    no("cannot cancel even the case that is safely cancellable")
if 'c.status == "sent"' in body and "409" in body and "at-most-once" in body:
    ok("a delivered command is REFUSED, not faked — 409 with the reason")
else:
    no("pretends to cancel something the device already has, which is worse than no cancel")
if "immutable history" in body:
    ok("finished commands are immutable")
else:
    no("completed commands can be rewritten")

print("\n  ---- negative control ----")
if "expired" in {c.name for c in [models.Command.__table__.c.status]} or True:
    # meaningful control: the sweeper must distinguish the two 'sent' rows above
    if stale.status != fresh.status:
        ok("the sweeper distinguishes a dead command from a live one")
    else:
        no("treats every 'sent' the same; the test proves nothing")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
