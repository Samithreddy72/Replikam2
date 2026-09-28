#!/usr/bin/env python3
"""Who a sign-in belongs to, when a bridge is handed a mesh key, and claiming once (2026-09-28).

  1. Revoking a user deleted the user row and kept their sign-ins. On the live database SQLite
     gives the next new user the revoked user's id, so the revoked person's token signed in as
     whoever was added next - an admin, if an admin was added next.
  2. Mesh self-heal minted a new tailnet key on EVERY 15 s poll while a bridge had no mesh
     address: thousands of Tailscale API calls and audit rows a day, a working node re-keyed
     (--reset) by a single empty sample or by a join slower than one tick, and LAN-only
     deployments re-keyed forever.
  3. Claim accepted a bridge that was already claimed (a new key, so its mesh reset mid-meeting),
     took any name, and two claims at once could both get the same fleet number.

Runs the real app in-process (FastAPI TestClient) on a throwaway SQLite database that starts
with the tables EXACTLY as a fleet created before 2026-09-28 has them - no AUTOINCREMENT, no
mesh columns, no fleet-number index - so the upgrade path is what gets tested. The Tailscale
API is replaced by a counter: nothing leaves this machine, no bridge or fleet is contacted."""
import os, pathlib, sys

try:
    import fastapi, httpx  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIP  FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import datetime as dt, hashlib, sqlite3, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "control-plane/backend"
T = pathlib.Path(tempfile.mkdtemp(prefix="nb-identity-"))
DB = T / "fleet.db"
os.environ.update(DATABASE_URL="sqlite:///%s" % DB, BOOTSTRAP_TOKENS="boot-test",
                  PAYLOAD_DIR=str(T / "payloads"), OFFLINE_AFTER_S="60", ALERT_EVAL_INTERVAL_S="3600",
                  TS_API_KEY="", NB_LAN_ONLY="")
os.environ.pop("NB_API_DOCS", None)
sys.path.insert(0, str(BACKEND))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:300]) if detail else ""))

def h(tok):
    return hashlib.sha256(tok.encode()).hexdigest()

# ---- a database as the live fleet has it: created by the build before 2026-09-28 ---------------
LEGACY = """
CREATE TABLE users (id INTEGER NOT NULL, email VARCHAR NOT NULL, org_id VARCHAR NOT NULL,
  role VARCHAR NOT NULL, invite_hash VARCHAR, token_hash VARCHAR, login_hash VARCHAR,
  login_expires DATETIME, created_at DATETIME NOT NULL, last_seen DATETIME, PRIMARY KEY (id));
CREATE INDEX ix_users_org_id ON users (org_id);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE INDEX ix_users_login_hash ON users (login_hash);
CREATE INDEX ix_users_token_hash ON users (token_hash);
CREATE TABLE sessions (id INTEGER NOT NULL, user_id INTEGER NOT NULL, token_hash VARCHAR NOT NULL,
  label VARCHAR NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id));
CREATE INDEX ix_sessions_user_id ON sessions (user_id);
CREATE INDEX ix_sessions_token_hash ON sessions (token_hash);
CREATE TABLE devices (id VARCHAR NOT NULL, org_id VARCHAR NOT NULL, pairing_code VARCHAR NOT NULL,
  name VARCHAR, number INTEGER, claimed_at DATETIME, hostname VARCHAR, version VARCHAR,
  tailscale_ip VARCHAR, token_hash VARCHAR, setup_pass VARCHAR, last_seen DATETIME, latest JSON,
  provision JSON, created_at DATETIME NOT NULL, PRIMARY KEY (id));
CREATE INDEX ix_devices_pairing_code ON devices (pairing_code);
CREATE INDEX ix_devices_number ON devices (number);
"""
con = sqlite3.connect(DB)
con.executescript(LEGACY)
con.execute("INSERT INTO users VALUES (1,'admin@test','default','admin',NULL,?,NULL,NULL,"
            "'2026-09-01 00:00:00.000000',NULL)", (h("ADMIN"),))
# left behind by an earlier revoke: its user (id 2) is gone - the next user created will be id 2
con.execute("INSERT INTO sessions VALUES (1,2,?,'sign-in','2026-09-02 00:00:00.000000')", (h("GHOST"),))
# older than the account it points at: id 1 was already given to somebody else
con.execute("INSERT INTO sessions VALUES (2,1,?,'sign-in','2026-08-01 00:00:00.000000')", (h("STALE"),))
# a genuine sign-in of the admin, after the account was made: must survive
con.execute("INSERT INTO sessions VALUES (3,1,?,'app','2026-09-03 00:00:00.000000')", (h("ADMIN2"),))
con.execute("INSERT INTO devices (id, org_id, pairing_code, name, number, claimed_at, created_at) VALUES "
            "('10000000cccc0001','default','BRIDGE-C001','Lobby',1,'2026-09-01 00:00:00','2026-09-01 00:00:00')")
con.commit(); con.close()

from app import main as M                       # migrates the legacy tables and purges at start-up
from app import mesh as MESH
from app.db import SessionLocal, Base
from app.models import Device, AuditLog, Session as UserSession, utcnow
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

MINTS, FAIL = [], {"on": False}
def fake_mint(description, tags=None):
    MINTS.append(description)
    if FAIL["on"]:
        raise MESH.MeshError("tailscale API unreachable (test)")
    return {"key": "tskey-auth-TEST-%d" % len(MINTS), "expires": None}
MESH.mint_ephemeral_key = fake_mint

c = TestClient(M.app)
A = {"Authorization": "Bearer ADMIN"}
def rows(sql, *a):
    k = sqlite3.connect(DB)
    try:
        return k.execute(sql, a).fetchall()
    finally:
        k.close()

print("  ---- 1. a revoked user's sign-in dies with them ----")
left = sorted(r[0] for r in rows("SELECT id FROM sessions"))
check(left == [3], "start-up removes sign-ins whose user is gone or newer than them, keeps the genuine one", left)
check(c.get("/auth/whoami", headers={"Authorization": "Bearer ADMIN2"}).status_code == 200,
      "…the genuine sign-in still works")

r = c.post("/admin/users", headers=A, json={"email": "bob@example.test", "role": "presenter"})
check(r.status_code == 200, "the admin invites bob (a presenter)", r.text)
bob_tok = c.post("/auth/redeem", json={"invite": r.json().get("invite", "")}).json().get("token", "")
BOB = {"Authorization": "Bearer " + bob_tok}
r = c.get("/auth/whoami", headers=BOB)
check(r.json().get("who") == "bob@example.test", "bob redeems the invite and signs in", r.text)
bob_id = next(u["id"] for u in c.get("/admin/users", headers=A).json() if u["email"] == "bob@example.test")
r = c.delete("/admin/users/%d" % bob_id, headers=A)
check(r.status_code == 200, "the admin revokes bob", r.text)
check(rows("SELECT COUNT(*) FROM sessions WHERE user_id=?", bob_id)[0][0] == 0,
      "…and bob's sign-ins are deleted with him")
check(c.get("/auth/whoami", headers=BOB).status_code == 401, "bob's token is refused at once")

r = c.post("/admin/users", headers=A, json={"email": "carol@example.test", "role": "admin"})
carol_invite = r.json().get("invite", "")
carol_id = next(u["id"] for u in c.get("/admin/users", headers=A).json() if u["email"] == "carol@example.test")
check(carol_id == bob_id, "the live table hands carol (an admin) bob's old id %d - the condition the attack needs" % bob_id,
      carol_id)
r = c.get("/auth/whoami", headers=BOB)
check(r.status_code == 401, "bob's old token does NOT sign in as carol", r.text)
check(c.get("/admin/devices", headers=BOB).status_code == 401, "…and cannot reach the admin API")
check(c.get("/auth/whoami", headers={"Authorization": "Bearer GHOST"}).status_code == 401,
      "a sign-in left behind by an earlier revoke does not become carol's either")
# a leftover the start-up purge could not have seen: refused on use, and removed
k = sqlite3.connect(DB)
k.execute("INSERT INTO sessions (user_id, token_hash, label, created_at) VALUES (?,?,'sign-in',?)",
          (carol_id, h("LEFTOVER"), "2026-09-05 00:00:00.000000"))
k.commit(); k.close()
check(c.get("/auth/whoami", headers={"Authorization": "Bearer LEFTOVER"}).status_code == 401,
      "a sign-in older than the account it points at is refused")
check(rows("SELECT COUNT(*) FROM sessions WHERE token_hash=?", h("LEFTOVER"))[0][0] == 0, "…and deleted")
carol_tok = c.post("/auth/redeem", json={"invite": carol_invite}).json().get("token", "")
r = c.get("/auth/whoami", headers={"Authorization": "Bearer " + carol_tok})
check(r.status_code == 200 and r.json().get("role") == "admin", "carol's own sign-in works (positive control)", r.text)

fresh = create_engine("sqlite:///%s" % (T / "fresh.db"))
Base.metadata.create_all(fresh)
with fresh.begin() as conn:
    ddl = dict(conn.execute(text("SELECT name, sql FROM sqlite_master WHERE type='table'")).fetchall())
    check("AUTOINCREMENT" in ddl["users"] and "AUTOINCREMENT" in ddl["sessions"],
          "a new database never reuses a user or session id (AUTOINCREMENT)")
    conn.execute(text("INSERT INTO users (email, org_id, role, created_at) VALUES ('x@t','default','presenter','2026-09-28')"))
    first = conn.execute(text("SELECT max(id) FROM users")).scalar()
    conn.execute(text("DELETE FROM users WHERE id=:i"), {"i": first})
    conn.execute(text("INSERT INTO users (email, org_id, role, created_at) VALUES ('y@t','default','admin','2026-09-28')"))
    second = conn.execute(text("SELECT max(id) FROM users")).scalar()
    check(second != first, "…deleting the newest user and adding another gives a new id (%s -> %s)" % (first, second))

print("\n  ---- 2. mesh self-heal: a key when needed, never a flood ----")
def enroll(serial, code):
    r = c.post("/v1/enroll", json={"bootstrap_token": "boot-test", "device_id": serial, "pairing_code": code,
                                   "version": "2.2.0-test", "hostname": code.lower()})
    return {"Authorization": "Bearer " + r.json()["device_token"]}

def tick(tok, ip=None):
    """One agent tick as the fleet sees it: telemetry, then the provision poll."""
    c.post("/v1/telemetry", headers=tok, json={"version": "2.2.0-test", "tailscale_ip": ip or ""})
    return c.get("/v1/provision", headers=tok).json()["provision"]

def backdate(serial, **minutes):
    s = SessionLocal()
    d = s.get(Device, serial)
    for col, mins in minutes.items():
        setattr(d, col, utcnow() - dt.timedelta(minutes=mins))
    s.commit(); s.close()

def device(serial):
    s = SessionLocal()
    try:
        return s.get(Device, serial)
    finally:
        s.close()

def auto_audits():
    s = SessionLocal()
    try:
        return len(s.scalars(select(AuditLog).where(AuditLog.action == "mesh-key:auto-reissue")).all())
    finally:
        s.close()

MS = "20000000dddd0001"
mtok = enroll(MS, "BRIDGE-M001")
r = c.post("/admin/devices/%s/claim" % MS, headers=A, json={"name": "Mesh Room"})
check(r.status_code == 200 and len(MINTS) == 1, "claim issues the bridge its first mesh key", (r.status_code, len(MINTS)))
p = tick(mtok)
check((p or {}).get("tailscale_auth_key") == "tskey-auth-TEST-1", "the bridge collects the claim key on its next tick", p)
got = [tick(mtok) for _ in range(10)]
check(not any(got) and len(MINTS) == 1,
      "10 more ticks while it is still joining: no second key (it used to get one every 15 s)", len(MINTS))
check(auto_audits() == 0, "…and nothing is written to the audit log")

backdate(MS, mesh_lost_at=11, mesh_key_at=11)          # ten minutes on, still not on the mesh
p = tick(mtok)
check((p or {}).get("tailscale_auth_key") == "tskey-auth-TEST-2" and p.get("tailscale_hostname") == "netbridge-M001",
      "still no mesh address 10 minutes after its last key: the fleet issues a new one", p)
check(auto_audits() == 1, "…audited once, as the system")
got = [tick(mtok) for _ in range(20)]
check(not any(got) and len(MINTS) == 2, "20 more ticks: no key flood (at most one key per 10 minutes)", len(MINTS))
backdate(MS, mesh_lost_at=21, mesh_key_at=11)
check(bool(tick(mtok)) and len(MINTS) == 3, "10 minutes later, still missing: one more key")
check(auto_audits() == 1, "…the same outage is not audited twice (144 rows a day would bury real actions)",
      auto_audits())

check(tick(mtok, ip="100.64.1.5") is None, "the bridge reports a mesh address: nothing to hand out")
d = device(MS)
check(getattr(d, "mesh_lost_at", "no column") is None and getattr(d, "mesh_autokeys", "no column") == 0,
      "…and the outage is closed", (getattr(d, "mesh_lost_at", "no column"), getattr(d, "mesh_autokeys", "no column")))
backdate(MS, mesh_key_at=30)
check(tick(mtok) is None and len(MINTS) == 3,
      "ONE empty sample (tailscaled restarting) does not re-key a working node", len(MINTS))
check(tick(mtok, ip="100.64.1.5") is None, "…and it is back on the next report")

tick(mtok)
backdate(MS, mesh_lost_at=2, mesh_key_at=11)
check(bool(tick(mtok)) and len(MINTS) == 4, "a real outage (no address for over a minute) is healed")
check(auto_audits() == 2, "…and a NEW outage is audited again", auto_audits())

FAIL["on"] = True
backdate(MS, mesh_key_at=11)
n = len(MINTS)
check(tick(mtok) is None and len(MINTS) == n + 1, "Tailscale unreachable: one attempt, nothing handed out")
got = [tick(mtok) for _ in range(5)]
check(len(MINTS) == n + 1, "…and it is NOT retried on every 15 s poll", len(MINTS) - n)
FAIL["on"] = False

M.LAN_ONLY = True
try:
    n = len(MINTS)
    tick(mtok, ip="100.64.1.5")
    backdate(MS, mesh_lost_at=5, mesh_key_at=60)
    got = [tick(mtok) for _ in range(3)]
    check(not any(got) and len(MINTS) == n, "NB_LAN_ONLY: never re-keys a bridge onto the mesh", len(MINTS) - n)
    tick(mtok, ip="100.64.1.7")                 # the LAST report carries an address: it must still not be kept
    check(device(MS).tailscale_ip is None, "NB_LAN_ONLY: never stores a mesh address, even from the latest report",
          device(MS).tailscale_ip)
finally:
    M.LAN_ONLY = False

# The stamp when a bridge COLLECTS a staged key (pull_provision) is what stops a second key for a join that takes
# longer than one tick after claim. Without it a bridge that had been off the mesh for a while is re-keyed
# (tailscale up --reset) on the very next tick while it is still joining with the first key.
MC = "20000000eeee0003"
ctok = enroll(MC, "BRIDGE-E003")
tick(ctok)                                            # enrolled, no mesh address yet
backdate(MC, mesh_lost_at=15)                         # long enough off the mesh that self-heal would be due
n = len(MINTS)
r = c.post("/admin/devices/%s/claim" % MC, headers=A, json={"name": "Late Room"})
p = tick(ctok)
check(r.status_code == 200 and (p or {}).get("tailscale_auth_key") == "tskey-auth-TEST-%d" % (n + 1),
      "claimed after a while off the mesh: the bridge collects its claim key", p)
check("_key_expires" not in (p or {}), "…the fleet's private expiry note is never sent to the bridge", p)
got = [tick(ctok) for _ in range(5)]
check(not any(got) and len(MINTS) == n + 1,
      "…and is NOT given a second key while it joins (collecting the key starts the re-key gap)", len(MINTS) - n)

# A staged key that EXPIRED while the bridge was off is replaced on collection, instead of being handed over dead
# and costing a failed join plus a whole re-key gap.
MD = "20000000eeee0004"
dtok = enroll(MD, "BRIDGE-E004")
r = c.post("/admin/devices/%s/claim" % MD, headers=A, json={"name": "Venue Room"})
staged = dict(device(MD).provision or {})
check(r.status_code == 200 and staged.get("_key_expires"), "claim records when the minted key expires (fleet side)", staged)
s = SessionLocal(); d = s.get(Device, MD)
d.provision = dict(staged, _key_expires=(utcnow() - dt.timedelta(hours=3)).isoformat())
s.commit(); s.close()
n = len(MINTS)
p = tick(dtok)
check((p or {}).get("tailscale_auth_key") and p["tailscale_auth_key"] != staged["tailscale_auth_key"] and len(MINTS) == n + 1,
      "powered on hours after claim: the dead staged key is replaced by a fresh one", (p, len(MINTS) - n))
check((p or {}).get("tailscale_hostname") == "netbridge-E004" and "_key_expires" not in (p or {}),
      "…with its hostname kept and nothing private in the payload", p)
check(device(MD).provision is None and not any(tick(dtok) for _ in range(3)) and len(MINTS) == n + 1,
      "…handed out once; no key flood afterwards", len(MINTS) - n)
for serial in (MC, MD):                                # leave the fleet exactly as the tests below expect it
    r = c.delete("/admin/devices/%s" % serial, headers=A)
    check(r.status_code == 200, "cleanup: test bridge %s removed" % serial, r.text)

print("\n  ---- 3. a bridge is claimed once ----")
n = len(MINTS)
r = c.post("/admin/devices/%s/claim" % MS, headers=A, json={"name": "Renamed by script"})
check(r.status_code == 409 and "already claimed" in r.text, "claiming a claimed bridge is refused (409)", r.text)
check("PATCH" in r.text and "mesh-key" in r.text, "…and the answer says how to rename or re-key instead")
d = device(MS)
check(len(MINTS) == n and d.provision is None and d.name == "Mesh Room",
      "…no new key is minted or staged, the name is unchanged", (len(MINTS) - n, d.provision, d.name))

S2, S3, S4 = "20000000dddd0002", "20000000dddd0003", "20000000dddd0004"
enroll(S2, "BRIDGE-M002"); enroll(S3, "BRIDGE-M003"); enroll(S4, "BRIDGE-M004")
for bad in ("", "   ", "x" * 65):
    r = c.post("/admin/devices/%s/claim" % S2, headers=A, json={"name": bad})
    check(r.status_code == 400, "claim refuses the name %r (1-64 characters, like rename)" % bad[:8], r.status_code)
check(device(S2).claimed_at is None, "…and the bridge stays unclaimed")

real_next = M._next_number
calls = []
def racing_next(db, org):
    calls.append(org)
    return 1 if len(calls) == 1 else real_next(db, org)    # first read: NB-001, already Lobby's
M._next_number = racing_next
try:
    r = c.post("/admin/devices/%s/claim" % S4, headers=A, json={"name": "  Studio  "})
finally:
    M._next_number = real_next
nums = [x[0] for x in rows("SELECT number FROM devices WHERE org_id='default' AND number IS NOT NULL")]
check(calls and r.status_code == 200 and len(nums) == len(set(nums)),
      "two claims picking the same next number: the database refuses the second, which retries",
      (len(calls), r.text[:120], nums))
check(r.status_code == 200 and r.json().get("label") != "NB-001" and r.json().get("name") == "Studio",
      "…the bridge gets a free number and its trimmed name", r.json() if r.status_code == 200 else r.text)
idx = [x[1] for x in rows("PRAGMA index_list(devices)")]
check("uq_devices_org_number" in idx, "the upgraded live table has the unique fleet-number index", idx)
s = SessionLocal()
s.get(Device, S3).number = 1
try:
    s.commit(); dup_ok = True
except IntegrityError:
    s.rollback(); dup_ok = False
s.close()
check(not dup_ok, "the database itself refuses a second NB-001 in the org")

dupdb = create_engine("sqlite:///%s" % (T / "dups.db"))
with dupdb.begin() as conn:
    for stmt in LEGACY.split(";"):
        if "devices" in stmt and stmt.strip():
            conn.execute(text(stmt))
    for i in (1, 2):
        conn.execute(text("INSERT INTO devices (id, org_id, pairing_code, number, created_at) "
                          "VALUES (:i, 'default', 'BRIDGE-D', 5, '2026-09-01')"), {"i": "dup-%d" % i})
ensure = getattr(M, "_ensure_fleet_number_index", None)
if ensure is None:
    check(False, "the migration can add the fleet-number index to an existing table", "no such helper")
else:
    with dupdb.begin() as conn:
        made = ensure(conn)
        have = [x[1] for x in conn.execute(text("PRAGMA index_list(devices)")).fetchall()]
    check(made is False and "uq_devices_org_number" not in have,
          "a fleet that already has two NB-005s still starts (the index waits, nobody is renumbered for them)")
    with dupdb.begin() as conn:
        conn.execute(text("UPDATE devices SET number = 6 WHERE id = 'dup-2'"))
        made = ensure(conn)
        have = [x[1] for x in conn.execute(text("PRAGMA index_list(devices)")).fetchall()]
    check(made is True and "uq_devices_org_number" in have, "…and gets the index once an admin has renumbered one")

n = len(MINTS)
r = c.post("/admin/devices/%s/mesh-key" % MS, headers=A, json={"tailscale_auth_key": "hand-made-key-0001"})
check(r.status_code == 200 and len(MINTS) == n, "re-keying with a hand-made key (what re-claim was used for) works", r.text)
check((tick(mtok) or {}).get("tailscale_auth_key") == "hand-made-key-0001", "…and the bridge receives that key")
r = c.post("/admin/devices/%s/mesh-key" % MS, headers=A, json={"tailscale_auth_key": "two words"})
check(r.status_code == 400, "a key with a space in it is refused", r.status_code)
r = c.post("/admin/devices/%s/mesh-key" % MS, headers=A)
check(r.status_code == 200 and len(MINTS) == n + 1, "the panel's re-key (no body) still mints one", r.text)

print("  ---- the organisation is never locked out ----")
r = c.delete("/admin/users/1", headers=A)                       # admin@test removing their own account
check(r.status_code == 409 and "your own account" in r.text and c.get("/auth/whoami", headers=A).status_code == 200,
      "an admin cannot remove their own account (another admin can)", r.text)
r = c.delete("/admin/users/%d" % carol_id, headers=A)
check(r.status_code == 200, "one of two admins can be removed", r.text)
from fastapi import HTTPException as _HE
from app.db import SessionLocal as _SL
_db = _SL()
try:
    M.revoke_user(1, actor=M.auth.Actor("fleet-key", "default", "admin", is_bootstrap=True), db=_db)
    last = None
except _HE as e:
    last = e
finally:
    _db.close()
check(last is not None and last.status_code == 409 and "last admin" in str(last.detail)
      and c.get("/auth/whoami", headers=A).status_code == 200,
      "the last admin cannot be removed - not even with the fleet's own key - or nobody could manage it", last)

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
