#!/usr/bin/env python3
"""What the fleet server keeps, and for how long, run for real (2026-09-28).

  - a bridge's diagnostics: the newest 3 bundles, not 4 (autoflush=False hid the new row)
  - the retention sweep: every finished command state (not only the four it listed), resolved
    alert episodes, and the sign-in sessions of users that no longer exist - while keeping what
    must stay: pending commands, commands a rollout still points at, open alert episodes

Starts the real control plane (uvicorn) on a throwaway SQLite database. The sweep checked here is
the one the server runs by itself at start-up, so this also proves the start-up hook still fires
on the installed FastAPI/Starlette. Needs FastAPI: uses ~/netbridge/fleet-test-venv when the
Python running it lacks it; skips (and says so) when neither has it."""
import os, pathlib, sys

try:
    import fastapi, httpx, uvicorn  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIPPED - FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import base64, datetime as dt, socket, subprocess, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "control-plane/backend"
T = pathlib.Path(tempfile.mkdtemp())
ENV = dict(os.environ, DATABASE_URL="sqlite:///%s" % (T / "fleet.db"), BOOTSTRAP_TOKENS="boot-test",
           PAYLOAD_DIR=str(T / "payloads"), OFFLINE_AFTER_S="600", ALERT_EVAL_INTERVAL_S="3600")
os.environ.update(ENV)
sys.path.insert(0, str(BACKEND))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:400]) if detail else ""))

from app import main as M  # noqa: F401  (creates the tables, migrates)
from app.models import (User, Device, Command, AlertEvent, DiagBundle, Rollout, RolloutTarget,
                        Session as UserSession)
from app.auth import hash_token
from app.db import SessionLocal

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
OLD = NOW - dt.timedelta(days=40)                 # past the 30-day command window
db = SessionLocal()
admin = User(email="admin@test", role="admin", org_id="default", token_hash=hash_token("ADMIN"))
db.add(admin)
# disk_low is firing on this bridge, so the server's own alert pass at start-up keeps the open
# disk_low episode below open instead of resolving it.
db.add(Device(id="dev-a", org_id="default", pairing_code="BRIDGE-AAAA", number=1, name="Hall",
              claimed_at=NOW, last_seen=NOW, latest={"data_free_mb": 100}))
db.commit()

# ---- what the sweep finds when the server starts -----------------------------------------------
STATES = ("done", "succeeded", "failed", "rejected", "cancelled", "expired", "sent", "pending")
old_cmd = {}
for s in STATES:
    c = Command(device_id="dev-a", type="running", status=s, created_at=OLD)
    db.add(c); db.flush(); old_cmd[s] = c.id
recent = Command(device_id="dev-a", type="running", status="expired", created_at=NOW - dt.timedelta(days=1))
db.add(recent); db.flush()

def rollout(version, target_status, cmd_status):
    """A rollout whose one target points at an update command 40 days old."""
    c = Command(device_id="dev-a", type="update", status=cmd_status, created_at=OLD, timeout_s=3600)
    ro = Rollout(version=version, source="https://example.invalid/ota", stage_pct=100, status="active",
                 created_at=OLD, updated_at=OLD)
    db.add_all([c, ro]); db.flush()
    db.add(RolloutTarget(rollout_id=ro.id, device_id="dev-a", status=target_status, command_id=c.id, wave=100))
    return ro.id, c.id
# still waiting: the rollout reads these results to decide how the bridge's update ended
ro_exp, awaited_exp = rollout("2.2.0-test", "dispatched", "expired")
ro_ok, awaited_ok = rollout("2.2.1-test", "dispatched", "done")
# already decided: the target still names its command (and the foreign key still points at it)
ro_old, decided = rollout("2.1.9-test", "succeeded", "done")

ev_old = AlertEvent(device_id="dev-a", kind="usb_misses", opened_at=NOW - dt.timedelta(days=100),
                    resolved_at=NOW - dt.timedelta(days=99))
ev_recent = AlertEvent(device_id="dev-a", kind="usb_misses", opened_at=NOW - dt.timedelta(days=10),
                       resolved_at=NOW - dt.timedelta(days=10) + dt.timedelta(hours=1))
ev_open = AlertEvent(device_id="dev-a", kind="disk_low", opened_at=NOW - dt.timedelta(days=200))
db.add_all([ev_old, ev_recent, ev_open]); db.flush()

# A session left behind by a user who no longer exists, and one of a user who does.
ghost = UserSession(user_id=987654, token_hash=hash_token("GHOST"), label="sign-in",
                    created_at=NOW - dt.timedelta(days=5))
own = UserSession(user_id=admin.id, token_hash=hash_token("OWN"), label="sign-in")   # signed in after the account was made
db.add_all([ghost, own]); db.commit()
ids = {"recent": recent.id, "ev_old": ev_old.id, "ev_recent": ev_recent.id, "ev_open": ev_open.id,
       "ghost": ghost.id, "own": own.id}

# ---- a real server ---------------------------------------------------------------------------
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--log-level", "warning"], cwd=str(BACKEND), env=ENV)
BASE = "http://127.0.0.1:%d" % PORT
for _ in range(100):
    try:
        if httpx.get(BASE + "/healthz", timeout=1).status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.1)
A = {"Authorization": "Bearer ADMIN"}
bearer = lambda t: {"Authorization": "Bearer " + t}
c = httpx.Client(base_url=BASE, timeout=10)

try:
    print("The sweep the server runs at start-up")
    print("=====================================")
    deadline = time.time() + 15            # 'done' rows were pruned before this fix too: wait for them
    while time.time() < deadline:
        db.expire_all()
        if db.get(Command, old_cmd["done"]) is None:
            break
        time.sleep(0.2)
    db.expire_all()
    check(db.get(Command, old_cmd["done"]) is None, "the start-up sweep ran (a 40-day-old finished command is gone)")
    for st in ("succeeded", "cancelled", "expired"):
        check(db.get(Command, old_cmd[st]) is None, "a 40-day-old %s command is pruned" % st)
    for st in ("failed", "rejected", "sent"):
        check(db.get(Command, old_cmd[st]) is None, "a 40-day-old %s command is pruned (as before)" % st)
    check(db.get(Command, old_cmd["pending"]) is not None, "a never-delivered (pending) command is kept at any age")
    check(db.get(Command, ids["recent"]) is not None, "a recent expired command is kept")
    for cid, what in ((awaited_exp, "expired"), (awaited_ok, "done")):
        check(db.get(Command, cid) is not None, "an old %s command a rollout is still waiting on is kept" % what)
    check(db.get(Command, decided) is not None, "an old command a finished rollout target still names is kept")
    for rid in (ro_exp, ro_ok):
        r = c.get("/admin/rollouts/%d" % rid, headers=A)
        db.expire_all()
        t = db.query(RolloutTarget).filter(RolloutTarget.rollout_id == rid).first()
        check(r.status_code == 200 and t is not None and t.status != "dispatched",
              "…and rollout %d still reads its result: the bridge no longer shows as updating" % rid,
              (r.status_code, t.status if t else None))
    check(db.get(AlertEvent, ids["ev_old"]) is None, "an alert episode resolved 99 days ago is pruned")
    check(db.get(AlertEvent, ids["ev_recent"]) is not None, "an episode resolved 10 days ago is kept")
    ev = db.get(AlertEvent, ids["ev_open"])
    check(ev is not None and ev.resolved_at is None, "an OPEN episode is never pruned, however old")
    check(db.get(UserSession, ids["ghost"]) is None, "a session whose user no longer exists is pruned")
    check(db.get(UserSession, ids["own"]) is not None, "a session of an existing user is kept")
    r = c.get("/auth/whoami", headers=bearer("OWN"))
    check(r.status_code == 200 and r.json().get("who") == "admin@test", "…and still signs in", r.text)

    print("\nDiagnostics bundles")
    print("===================")
    r = c.post("/v1/enroll", json={"bootstrap_token": "boot-test", "device_id": "10000000dddd0001",
                                   "pairing_code": "BRIDGE-DDDD", "version": "2.2.0-test", "hostname": "nb-d"})
    dev = bearer(r.json()["device_token"])
    names = ["bundle-20260928-%06d.tgz" % (120000 + i) for i in range(6)]
    codes = [c.post("/v1/diagnostics", headers=dev,
                    json={"filename": n, "data_b64": base64.b64encode(b"tgz %d" % i).decode()}).status_code
             for i, n in enumerate(names)]
    check(codes == [200] * 6, "a bridge uploads 6 bundles", codes)
    db.expire_all()
    kept = sorted(b.filename for b in db.query(DiagBundle).filter(DiagBundle.device_id == "10000000dddd0001"))
    check(kept == names[-3:], "only the newest 3 are stored, not 4", kept)
    listed = c.get("/admin/devices/10000000dddd0001/diagnostics", headers=A).json()
    check([b["filename"] for b in listed] == names[:2:-1], "…and the panel lists those 3, newest first",
          [b["filename"] for b in listed])
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
