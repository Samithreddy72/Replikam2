#!/usr/bin/env python3
"""The command queue must never answer for a DIFFERENT command than the one asked for.

Found by the 2026-09-28 audit, all on the fleet's command path:

  * the in-flight guard matched on the command TYPE only: `deploy bridge-agent.py` while
    `deploy bridge-web.py` was in flight got the bridge-web.py command back, nb printed "queued"
    and then "done", and bridge-agent.py never reached the bridge. Same for one PIN or one OS
    version swapped for another;
  * expiry ran only when someone read the command list, so a reboot whose result was lost stayed
    "sent" for ever and the next reboot was "deduplicated" onto that dead row;
  * a reboot queued for an offline bridge ran whenever it came back - days later, mid-meeting;
  * state changes were read -> check -> write: a cancel could overwrite the "sent" the bridge's
    poll had just committed (the panel said "cancelled" while the bridge rebooted), and the
    sweeper could turn a "done" into "expired";
  * broadcast had no retry guards, and reached unclaimed cards and bridges in a meeting;
  * a device could report any status word, and "pending" put the command back in the queue.

Drives the real FastAPI app in-process against a scratch SQLite database (the races need to
interleave a second writer at an exact point, which only an in-process test can do).
"""
import datetime as dt, os, pathlib, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BE = ROOT / "control-plane/backend"
TMP = tempfile.mkdtemp(prefix="nb-queue-")
os.environ["PAYLOAD_DIR"] = os.path.join(TMP, "payloads")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db")
os.environ.pop("NB_API_DOCS", None)
sys.path.insert(0, str(BE))

P = F = 0


def ok(m):
    global P
    P += 1
    print("  PASS  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  FAIL  %s%s" % (m, ("\n        " + str(d)[:300]) if d else ""))


def check(cond, m, d=""):
    ok(m) if cond else no(m, d)


try:
    from fastapi.testclient import TestClient
    import app.main as M
    from app.db import SessionLocal
    from app.models import Device, Command, utcnow
    from sqlalchemy import select, update
    from sqlalchemy.orm import Session as _Session
except Exception as e:
    print("  SKIPPED - backend deps unavailable (%s)" % e)
    raise SystemExit(0)

print("NetBridge command queue")
print("=======================\n")

client = TestClient(M.app)


class _Actor:
    org, id, email, role = "org-q", "admin-q", "admin@q", "admin"


_dev = {"id": "q1"}
M.app.dependency_overrides[M.auth.require_admin] = lambda: _Actor()
M.app.dependency_overrides[M.auth.require_device] = lambda: type("D", (), {"id": _dev["id"]})()

PIN2 = {"pin_set": True, "required": True, "protocol": 2}
db = SessionLocal()
for i, name in ((1, "Studio A"), (2, "Studio B"), (3, "Hall"), (4, None)):
    db.add(Device(id="q%d" % i, org_id="org-q", name=name, pairing_code="BRIDGE-Q%03d" % i, number=i if name else None,
                  claimed_at=utcnow() if name else None, last_seen=utcnow(), version="2.2.1-aaaaaaa",
                  latest={"udc": "not attached", "streams": {"video": False}, "pin": PIN2}))
db.commit()


def cmds(dev="q1", ctype=None):
    s = SessionLocal()
    q = select(Command).where(Command.device_id == dev)
    if ctype:
        q = q.where(Command.type == ctype)
    out = [(c.id, c.type, c.status, dict(c.args or {})) for c in s.scalars(q.order_by(Command.id)).all()]
    s.close()
    return out


def row(cid):
    s = SessionLocal()
    c = s.get(Command, cid)
    out = (c.status, dict(c.args or {}), c.output or "", c.fail_reason or "") if c else None
    s.close()
    return out


def set_row(cid, **kw):
    s = SessionLocal()
    s.execute(update(Command).where(Command.id == cid).values(**kw))
    s.commit()
    s.close()


def issue(dev, ctype, args=None, **kw):
    return client.post("/admin/devices/%s/commands" % dev, json=dict({"type": ctype, "args": args or {}}, **kw))


def pull(dev):
    _dev["id"] = dev
    return client.get("/v1/commands").json()


def report(dev, cid, status, output="ok"):
    _dev["id"] = dev
    return client.post("/v1/commands/%s/result" % cid, json={"status": status, "output": output})


def finish(cid):
    set_row(cid, status="done", completed_at=utcnow())


ago = lambda s: utcnow() - dt.timedelta(seconds=s)

# ------------------------------------------------------------------------------------------------
print("  ---- dedup hands back only the SAME command (same type AND args) ----")
a = issue("q1", "deploy-script", {"name": "bridge-web.py", "source": "https://f/payloads"}, confirm=True)
b = issue("q1", "deploy-script", {"name": "bridge-agent.py", "source": "https://f/payloads"}, confirm=True)
check(a.status_code == 200 and b.status_code == 409, "a deploy of ANOTHER file while one is in flight is refused (409), not swapped",
      (a.status_code, b.status_code, b.text))
det = (b.json().get("detail") or {}) if b.status_code == 409 else {}
check(det.get("error") == "busy" and det.get("in_flight") == a.json().get("id")
      and "bridge-web.py" in det.get("detail", "") and "bridge-agent.py" in det.get("detail", ""),
      "…and says which command is in the way and what was NOT queued", det)
check([c[3].get("name") for c in cmds("q1", "deploy-script")] == ["bridge-web.py"],
      "…and nothing is queued for bridge-agent.py (no silent substitution, no concurrent deploy)",
      cmds("q1", "deploy-script"))
a2 = issue("q1", "deploy-script", {"source": "https://f/payloads", "name": "bridge-web.py"}, confirm=True)
check(a2.status_code == 200 and a2.json().get("id") == a.json().get("id") and a2.json().get("deduplicated") == "already_in_flight",
      "the identical deploy (args in any order) is still deduplicated onto the one in flight", a2.text)
finish(a.json()["id"])
b2 = issue("q1", "deploy-script", {"name": "bridge-agent.py", "source": "https://f/payloads"}, confirm=True)
check(b2.status_code == 200 and not b2.json().get("deduplicated") and b2.json()["id"] != a.json()["id"],
      "once the first finished, the other file queues as its own command", b2.text)

p1 = issue("q2", "set-pin", {"pin": "1111"})
p2 = issue("q2", "set-pin", {"pin": "2222"})
check(p1.status_code == 200 and p2.status_code == 409 and "1111" not in p2.text and "2222" not in p2.text,
      "a set-pin with ANOTHER PIN is refused while one is queued, and no PIN appears in the answer", p2.text)
check([c[3].get("pin") for c in cmds("q2", "set-pin")] == ["1111"], "…the bridge would receive only the first PIN, as asked",
      cmds("q2", "set-pin"))
u1 = issue("q3", "update", {"version": "2.2.2-bbbbbbb"}, confirm=True)
u2 = issue("q3", "update", {"version": "2.2.3-ccccccc"}, confirm=True)
check(u1.status_code == 200 and u2.status_code == 409 and "2.2.2-bbbbbbb" in u2.text,
      "an OS update to another version is refused while one is in flight (two would share the spare slot)", u2.text)
g1 = issue("q3", "gadget-tune", {"key": "UAC2_C_SYNC", "value": "async"})
g2 = issue("q3", "gadget-tune", {"key": "UAC2_P_SRATE", "value": "48000"})
check(g1.status_code == 200 and g2.status_code == 409, "gadget-tune with another key is refused, not swapped", g2.text)
d1 = issue("q3", "diagnose")
d2 = issue("q3", "diagnose")
check(d1.json()["id"] != d2.json()["id"], "read-only commands are still never deduplicated")

# ------------------------------------------------------------------------------------------------
print("\n  ---- expiry does not wait for somebody to open the panel ----")
r1 = issue("q1", "reboot", confirm=True).json()
pull("q1")                                              # delivered; the reboot killed the agent
set_row(r1["id"], sent_at=ago(5 * 3600))                # five hours later, nobody looked
r2 = issue("q1", "reboot", confirm=True)
check(r2.status_code == 200 and r2.json()["id"] != r1["id"] and not r2.json().get("deduplicated"),
      "a new reboot is QUEUED, not deduplicated onto yesterday's dead 'sent' row", r2.text)
check(row(r1["id"])[0] == "expired", "…and the dead row was expired on the way", row(r1["id"]))
set_row(r2.json()["id"], status="sent", sent_at=ago(5 * 3600))
check(client.get("/admin/devices/q1").json()["commands"][0]["status"] == "expired",
      "the device view expires a dead 'sent' row before showing it")
r3 = issue("q2", "restart").json()
pull("q2")
set_row(r3["id"], sent_at=ago(3600))
hk = getattr(M, "_housekeeping_once", None)      # absent before 2026-09-28: then this must FAIL, not crash
if hk:
    hk()
check(hk is not None and row(r3["id"])[0] == "expired", "the 30 s housekeeping timer expires it with no reader at all", row(r3["id"]))

# ------------------------------------------------------------------------------------------------
print("\n  ---- a disruptive command queued for an offline bridge goes stale ----")
old = issue("q3", "reboot", confirm=True).json()
set_row(old["id"], created_at=ago(3 * 24 * 3600))       # queued on Friday, bridge back on Monday
lk = issue("q3", "lock", confirm=True).json()
set_row(lk["id"], created_at=ago(3 * 24 * 3600))        # an admin lock queued as long ago
rd = issue("q3", "running").json()
set_row(rd["id"], created_at=ago(3 * 24 * 3600))        # a read waits for its bridge, as before
got = pull("q3")
ids = [c["id"] for c in got]
check(old["id"] not in ids, "the bridge coming back is NOT handed Friday's reboot", got)
check(rd["id"] in ids, "…but a read queued as long ago is still delivered (queue-until-online stays)", ids)
check(lk["id"] in ids and row(lk["id"])[0] == "sent",
      "…and so is an admin lock: arriving late it still keeps people out, so it never goes stale", (ids, row(lk["id"])))
st = row(old["id"])
check(st[0] == "expired" and "never collected" in st[3], "…the reboot is expired, and says why", st)
fresh = issue("q3", "restart").json()
check(fresh["id"] in [c["id"] for c in pull("q3")], "a restart queued minutes ago is delivered normally")
set_row(fresh["id"], status="done")
late = issue("q3", "restart").json()
set_row(late["id"], created_at=ago(2 * 3600))           # nothing sweeps between this and the poll:
got3 = [c["id"] for c in pull("q3")]                    # a bridge back after a weekend polls first
check(late["id"] not in got3 and row(late["id"])[0] == "expired",
      "…and one gone stale is refused by the bridge's poll itself, before any sweep has run", (got3, row(late["id"])))
uf = issue("q2", "update", {"version": "2.2.2-bbbbbbb", "force": True}, confirm=True).json()
set_row(uf["id"], created_at=ago(2 * 3600))
M._sweep_expired(SessionLocal())
check(row(uf["id"])[0] == "expired", "a FORCED OS update goes stale too (it would not wait for the meeting)", row(uf["id"]))
un = issue("q2", "update", {"version": "2.2.2-bbbbbbb"}, confirm=True).json()
set_row(un["id"], created_at=ago(2 * 3600))
M._sweep_expired(SessionLocal())
check(row(un["id"])[0] == "pending", "an ordinary OS update still waits (the bridge refuses it during a meeting itself)", row(un["id"]))
set_row(un["id"], status="cancelled")

# ------------------------------------------------------------------------------------------------
print("\n  ---- a device reports only done / failed / rejected ----")
x = issue("q1", "restart").json()
pull("q1")
report("q1", x["id"], "pending", "i am confused")
check(row(x["id"])[0] == "failed" and "'pending'" in row(x["id"])[2],
      "status 'pending' is recorded as failed (raw word kept), not put back in the queue", row(x["id"]))
check(x["id"] not in [c["id"] for c in pull("q1")], "…so it is never delivered a second time")
y = issue("q1", "profile", {"mode": "wan"}).json()
pull("q1")
report("q1", y["id"], "ok")
check(row(y["id"])[0] == "failed" and "'ok'" in row(y["id"])[2], "an unknown word ('ok') reaches a final state: failed", row(y["id"]))
z = issue("q1", "profile", {"mode": "wan"}).json()
pull("q1")
report("q1", z["id"], "done")
check(row(z["id"])[0] == "done", "a real result is still recorded as sent", row(z["id"]))


# ------------------------------------------------------------------------------------------------
print("\n  ---- state changes are conditional: the second writer sees that it lost ----")
class _Race:
    """After the endpoint has READ the target row, another writer commits `values` to it."""
    def __init__(self, cid, via, **values):
        self.cid, self.via, self.values, self.fired = cid, via, values, False

    def __enter__(self):
        race = self
        if self.via == "get":
            self.orig = orig = _Session.get

            def patched(sess, entity, ident, *a, **kw):
                obj = orig(sess, entity, ident, *a, **kw)
                if entity is Command and ident == race.cid and not race.fired:
                    race.fired = True
                    set_row(race.cid, **race.values)
                return obj
            _Session.get = patched
        else:
            self.orig = orig = _Session.scalars

            def patched(sess, stmt, *a, **kw):
                res = orig(sess, stmt, *a, **kw)
                if race.fired or getattr(stmt, "column_descriptions", [{}])[0].get("entity") is not Command:
                    return res
                rows = res.all()
                if any(getattr(r, "id", None) == race.cid for r in rows):
                    race.fired = True
                    set_row(race.cid, **race.values)
                return type("R", (), {"all": lambda self: rows, "__iter__": lambda self: iter(rows),
                                      "first": lambda self: rows[0] if rows else None})()
            _Session.scalars = patched
        return self

    def __exit__(self, *exc):
        setattr(_Session, self.via, self.orig)


c1 = issue("q2", "reboot", confirm=True).json()
with _Race(c1["id"], "get", status="sent", sent_at=utcnow()) as rc:
    r = client.delete("/admin/devices/q2/commands/%s" % c1["id"])
check(rc.fired, "(the race was staged: the bridge's poll committed 'sent' after the cancel read 'pending')")
check(r.status_code == 409 and row(c1["id"])[0] == "sent",
      "a cancel that loses to the bridge's poll answers 409, never 'cancelled' while the bridge reboots",
      (r.status_code, r.text[:160], row(c1["id"])))

c2 = issue("q3", "restart").json()
pull("q3")
set_row(c2["id"], sent_at=ago(3600))
with _Race(c2["id"], "scalars", status="done", output="restarted", completed_at=utcnow()) as rc:
    M._sweep_expired(SessionLocal())
check(rc.fired and row(c2["id"])[0] == "done",
      "a result that lands while the sweeper runs is not overwritten with 'expired'", row(c2["id"]))

c3 = issue("q3", "reset-clock", confirm=True).json()
pull("q3")
with _Race(c3["id"], "get", status="expired", fail_reason="deadline", completed_at=utcnow()) as rc:
    r = report("q3", c3["id"], "done", "finished late")
st = row(c3["id"])
check(rc.fired and st[0] == "expired" and "late report" in st[2] and r.json().get("recorded") == "late-report",
      "a result that loses to the sweeper becomes a late report; the verdict stands", (st, r.text[:120]))

c4 = issue("q1", "golden-save").json()
with _Race(c4["id"], "scalars", status="cancelled", completed_at=utcnow()) as rc:
    got = pull("q1")
check(rc.fired and c4["id"] not in [g["id"] for g in got] and row(c4["id"])[0] == "cancelled",
      "a command cancelled while the bridge polls is not handed to the bridge", (got, row(c4["id"])))

# ------------------------------------------------------------------------------------------------
print("\n  ---- broadcast has the same guards as one bridge ----")
for cid in [c[0] for d in ("q1", "q2", "q3") for c in cmds(d) if c[2] in ("pending", "sent")]:
    set_row(cid, status="done")
s = SessionLocal()
s.get(Device, "q2").latest = {"udc": "configured", "streams": {"video": False}, "pin": PIN2}
s.commit()
s.close()
b1 = client.post("/admin/commands/broadcast", json={"type": "restart", "idempotency_key": "k-1"})
q = b1.json()
names = [x["device"] for x in q.get("queued", [])]
skipped = {x["device"]: x["reason"] for x in q.get("skipped", [])}
check(b1.status_code == 200 and not any("Q004" in n for n in names) and any("Q004" in n for n in skipped),
      "an unclaimed card is left out of a broadcast, and named", q)
check(any("Studio B" in n for n in skipped) and "laptop" in next(v for k, v in skipped.items() if "Studio B" in k),
      "a bridge with its meeting laptop attached is not restarted by a broadcast", skipped)
b2 = client.post("/admin/commands/broadcast", json={"type": "restart", "idempotency_key": "k-1"})
first = sorted(x["command_id"] for x in q["queued"])
again = sorted(x["command_id"] for x in b2.json().get("queued", []))
check(first and first == again and all(x.get("deduplicated") for x in b2.json()["queued"]),
      "a retried broadcast (same idempotency key) returns the same commands - one restart each, not two",
      (first, b2.json()))
b3 = client.post("/admin/commands/broadcast", json={"type": "restart"})
check(sorted(x["command_id"] for x in b3.json().get("queued", [])) == first,
      "without a key, a broadcast while the same restart is in flight is deduplicated per bridge", b3.json())
b4 = client.post("/admin/commands/broadcast", json={"type": "lock", "confirm": True})
check(any("Studio B" in x["device"] for x in b4.json().get("queued", [])),
      "…but a broadcast LOCK still reaches a bridge in a meeting (ending sessions is its purpose)", b4.json())
b5 = client.post("/admin/commands/broadcast", json={"type": "update", "args": {"version": "2.1.0-9e88a12"}, "confirm": True})
sk5 = {x["device"]: x["reason"] for x in b5.json().get("skipped", [])}
check(b5.status_code == 200 and not b5.json().get("queued") and any("downgrade" in v for v in sk5.values()),
      "a broadcast OS update to an older version installs nowhere (each bridge is named as a downgrade)", b5.json())

# ------------------------------------------------------------------------------------------------
print("\n  ---- negative control ----")
ak = getattr(M, "_args_key", None)
check(ak is not None and ak({"a": 1, "b": 2}) == ak({"b": 2, "a": 1}) and ak({"a": 1}) != ak({"a": 2}),
      "the args comparison is order-blind and value-sensitive (it is a real comparison)")

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
