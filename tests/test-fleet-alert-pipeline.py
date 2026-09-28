#!/usr/bin/env python3
"""The alert pipeline pages once, for real problems, and says what it means (2026-09-28 audit).

Every section below is a failure the audit reproduced on the fleet code of 2026-09-25:
  - an offline blip emailed every other open alert RESOLVED, then FIRING again on return;
  - a reading at a threshold (USB misses 14-16/s, a power blip) emailed on every crossing;
  - a fleet restart paged every bridge CRITICAL offline; a one-minute reboot paged offline too;
  - a new SD card: "RESOLVED new_device" 30 s later, CRITICAL pin_not_set / offline for an
    unclaimed card, and the enrolment email sent while holding the database write lock;
  - restart_storm blind to brownout reboots, silenced by one odd counter, invisible in the panel;
  - three failed services named as one; failed delivery retried every tick for every alert;
  - a 'Clear the lockout' fix the fleet refuses on older bridges; a test alert that read as a
    CRITICAL outage; resolved emails that looked like a second page; the panel promising email
    it did not send; nb show printing raw dicts.
And what the first round of fixes still got wrong: the panel calling a bridge fine while its alert
was held open by hysteresis; one odd reading (a string, NaN, Infinity) or unreadable telemetry
silencing a bridge in the loop; a bridge forgotten mid-send making the next pass re-send a digest;
channel errors quoting the recipient or the app password; the upgrade of an older database.
And what the review of those fixes found: a digest email showing every bridge the FIRST bridge's
steps; a digest too long for Discord failing on every pass; a channel left dark for an hour after
four failures; a bridge back online with unreadable telemetry never closing its offline episode;
the clear window and a long outage with no test that failed without them; another organisation's
admin seeing where the operator's alerts go.
Starts the real control plane (uvicorn) on a throwaway SQLite database with a local webhook, and
runs the alert loop in-process with a clock it moves. Needs FastAPI (~/netbridge/fleet-test-venv)."""
import os, pathlib, sys

try:
    import fastapi, httpx, uvicorn  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIP  FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import datetime as dt, http.server, importlib.util, json, re, smtplib, socket, subprocess, tempfile, threading, time, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "control-plane/backend"
T = pathlib.Path(tempfile.mkdtemp())

# ---- a webhook both the server and the in-process loop deliver to ------------------------------
HOOK = {"got": [], "tries": 0, "delay": 0.0, "status": 200}
class _Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(HOOK["delay"])
        HOOK["tries"] += 1
        if HOOK["status"] == 200:
            HOOK["got"].append(json.loads(body or b"{}"))
        self.send_response(HOOK["status"]); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
_hook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
threading.Thread(target=_hook.serve_forever, daemon=True).start()
HOOK_URL = "http://127.0.0.1:%d/hook-secret-path" % _hook.server_address[1]

ENV = dict(os.environ, DATABASE_URL="sqlite:///%s" % (T / "fleet.db"), BOOTSTRAP_TOKENS="boot-test",
           PAYLOAD_DIR=str(T / "payloads"), OFFLINE_AFTER_S="60", ALERT_EVAL_INTERVAL_S="3600",
           ALERT_WEBHOOK_URL=HOOK_URL, ALERT_WEBHOOK_FORMAT="raw", PUBLIC_BASE_URL="https://fleet.example",
           SMTP_HOST="", SMTP_USER="", SMTP_PASSWORD="", ALERT_EMAIL_TO="", ALERT_EMAIL_FROM="")
os.environ.update(ENV)
sys.path.insert(0, str(BACKEND))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:500]) if detail else ""))

def attempt(fn):
    """fn() or the exception it raised - so a missing function on older code FAILS a check, not the run."""
    try:
        return fn()
    except Exception as e:
        return e

from app import main as M                        # creates tables, migrates
from app import alerting as AL, alerts as AS, notifier as NT
from app.config import settings as S
from app.models import User, Device, AlertEvent, Telemetry
from app.auth import hash_token
from app.db import SessionLocal

UTC = dt.timezone.utc
CLOCK = [dt.datetime.now(UTC)]                  # the alert loop's clock; each section moves it on
NT._now = lambda: CLOCK[0].timestamp()          # the channel back-off follows it too
import inspect
if "now" not in inspect.signature(AL.evaluate).parameters:
    # Code from before 2026-09-28 reads the wall clock. Give it this test's clock, so it fails the
    # checks below on its behaviour, not on a clock that has run ahead of the wall.
    AS.utcnow = AL.utcnow = lambda: CLOCK[0]
db = SessionLocal()
db.add_all([User(email="admin@test", role="admin", org_id="default", token_hash=hash_token("ADMIN")),
            User(email="presenter@test", role="presenter", org_id="default", token_hash=hash_token("PRES"))])
db.commit()

s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
                        "--log-level", "warning"], cwd=str(BACKEND), env=ENV)
BASE = "http://127.0.0.1:%d" % PORT
for _ in range(100):
    try:
        if httpx.get(BASE + "/healthz", timeout=1).status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.1)
A = {"Authorization": "Bearer ADMIN"}


def tick(secs=30, started_at=None):
    """Move the clock and run one alert pass in its own session, as evaluate_loop does."""
    CLOCK[0] += dt.timedelta(seconds=secs)
    s2 = SessionLocal()
    try:
        try:
            return AL.evaluate(s2, now=CLOCK[0], started_at=started_at)
        except TypeError:                      # code without a clock: the checks then fail on behaviour
            return AL.evaluate(s2)
    finally:
        s2.close()


def msgs(since=0, dev=None):
    """Messages the webhook received (digests opened up), optionally about one bridge."""
    out = []
    for p in HOOK["got"][since:]:
        for m in (p.get("items") or []) if p.get("event") == "digest" else [p]:
            if dev is None or (m.get("device") or {}).get("id") == dev:
                out.append(m)
    return out


def ek(ms):
    return [(m.get("event"), m.get("kind")) for m in ms]


def bridge(id_, number, name, latest=None, claimed=True, seen=0):
    d = Device(id=id_, org_id="default", pairing_code="BRIDGE-" + id_[-4:].upper(), number=number if claimed else None,
               name=name if claimed else None, claimed_at=CLOCK[0] if claimed else None,
               last_seen=CLOCK[0] - dt.timedelta(seconds=seen), latest=latest or {"pin": {"pin_set": True, "protocol": 2, "required": True}})
    db.add(d); db.commit()
    return d


def setdev(id_, latest=None, seen=0):
    db.expire_all()
    d = db.get(Device, id_)
    if latest is not None:
        d.latest = latest
    d.last_seen = CLOCK[0] - dt.timedelta(seconds=seen)
    db.commit()


def events(id_, kind=None):
    db.expire_all()
    q = db.query(AlertEvent).filter(AlertEvent.device_id == id_)
    if kind:
        q = q.filter(AlertEvent.kind == kind)
    return q.order_by(AlertEvent.id).all()


def drop(*ids):
    db.expire_all()
    for m in (AlertEvent, Telemetry):
        db.query(m).filter(m.device_id.in_(ids)).delete(synchronize_session=False)
    db.query(Device).filter(Device.id.in_(ids)).delete(synchronize_session=False)
    db.commit()


PIN_OK = {"pin_set": True, "protocol": 2, "required": True}

try:
    print("\nA new SD card")
    print("=============")
    r = httpx.post(BASE + "/v1/enroll", json={"bootstrap_token": "boot-test", "device_id": "20000000aaaa0b00",
                                              "pairing_code": "BRIDGE-0B00", "version": "2.2.0", "hostname": "nb-0b00"})
    tok0 = {"Authorization": "Bearer " + r.json()["device_token"]}
    httpx.post(BASE + "/v1/telemetry", headers=tok0, json={"version": "2.2.0"})
    start = len(HOOK["got"])
    HOOK["delay"] = 7.0                                    # a slow mail server / webhook
    res = {}
    def enroll_new():
        t0 = time.monotonic()
        rr = httpx.post(BASE + "/v1/enroll", timeout=30, json={"bootstrap_token": "boot-test", "device_id": "20000000aaaa0c01",
                                                                "pairing_code": "BRIDGE-C001", "version": "2.2.0", "hostname": "nb-c001"})
        res["enroll"] = (rr.status_code, time.monotonic() - t0)
    th = threading.Thread(target=enroll_new, daemon=True); th.start()
    time.sleep(1.0)
    t0 = time.monotonic()
    rt = httpx.post(BASE + "/v1/telemetry", headers=tok0, json={"version": "2.2.0"}, timeout=30)
    took = time.monotonic() - t0
    th.join(30)
    HOOK["delay"] = 0.0
    time.sleep(0.2)
    check(events("20000000aaaa0c01", "new_device") and all(e.resolved_at is not None for e in events("20000000aaaa0c01", "new_device")),
          "the enrolment is stored as a record (born resolved), not an open problem the loop will 'resolve'")
    check(res.get("enroll", (0, 99))[0] == 200 and res["enroll"][1] < 3,
          "a new card's enrolment answers at once while the alert channel is slow (%.1f s)" % res.get("enroll", (0, 99))[1], res)
    check(rt.status_code == 200 and took < 3,
          "another bridge's heartbeat is saved meanwhile - no 'database is locked' (%d in %.1f s)" % (rt.status_code, took), rt.text[:120])
    setdev("20000000aaaa0c01", latest={"pin": {"pin_set": False, "required": True, "protocol": 2}})
    for _ in range(5):
        setdev("20000000aaaa0c01"); tick()
    c1 = msgs(start, "20000000aaaa0c01")
    check(ek(c1) == [("firing", "new_device")], "the new card is announced exactly once, and never 'RESOLVED'", ek(c1))
    check(c1 and c1[0].get("severity") == "info" and c1[0]["device"]["name"] == "BRIDGE-C001",
          "…as info, named by its pairing code (the same name the panel uses)", c1[:1])
    check(not events("20000000aaaa0c01", "pin_not_set"), "an unclaimed card with no PIN yet is not paged CRITICAL pin_not_set")
    setdev("20000000aaaa0c01", seen=3 * 86400)
    n0 = len(HOOK["got"]); tick(); tick()
    check(not events("20000000aaaa0c01", "offline") and not msgs(n0, "20000000aaaa0c01"),
          "an unclaimed card switched off for 3 days is not paged offline", ek(msgs(n0, "20000000aaaa0c01")))
    db.add(AlertEvent(device_id="20000000aaaa0b00", kind="offline", detail="no heartbeat", opened_at=CLOCK[0] - dt.timedelta(days=2),
                      notified_at=CLOCK[0] - dt.timedelta(days=2)))            # what the older code left behind
    db.commit()
    n0 = len(HOOK["got"]); tick(); tick()
    old = events("20000000aaaa0b00", "offline")
    check(old and old[0].resolved_at is not None and not msgs(n0, "20000000aaaa0b00"),
          "an episode older code left open on an unclaimed card is closed, without an email", ek(msgs(n0, "20000000aaaa0b00")))
    httpx.post(BASE + "/v1/enroll", json={"bootstrap_token": "boot-test", "device_id": "20000000aaaa0c02",
                                          "pairing_code": "BRIDGE-C002", "version": "2.2.0", "hostname": "nb-c002"})
    db.expire_all()
    c2 = db.get(Device, "20000000aaaa0c02")
    c2.claimed_at, c2.name, c2.number, c2.last_seen = CLOCK[0], "Claimed fast", 8, CLOCK[0]
    c2.latest = {"pin": PIN_OK}
    db.commit()                                            # claimed before the loop's next pass
    n0 = len(HOOK["got"]); tick()
    check(not [m for m in msgs(n0, "20000000aaaa0c02") if m.get("kind") == "new_device"],
          "a card claimed before its notice went out is not announced (no news, and 'Claim it' would be wrong)",
          ek(msgs(n0, "20000000aaaa0c02")))
    drop("20000000aaaa0b00", "20000000aaaa0c01", "20000000aaaa0c02")

    print("\nOffline hides the other alerts - it does not fix them")
    print("=====================================================")
    bridge("dev-lab", 9, "Lab", latest={"temp": "80.0'C", "pin": {"pin_set": False, "required": True, "protocol": 2}})
    n0 = len(HOOK["got"])
    for _ in range(3):
        setdev("dev-lab"); tick()
    check(sorted(ek(msgs(n0, "dev-lab"))) == [("firing", "pin_not_set"), ("firing", "temp_high")],
          "Lab: No PIN set and Running hot are emailed once", ek(msgs(n0, "dev-lab")))
    before = {e.kind: e.id for e in events("dev-lab") if e.resolved_at is None}
    n0 = len(HOOK["got"])
    for _ in range(5):
        setdev("dev-lab", seen=120); tick()                   # off the Wi-Fi for 2.5 min: longer than the clear window
    check(ek(msgs(n0, "dev-lab")) == [("firing", "offline")],
          "it goes offline: ONE email (offline), no 'RESOLVED' for the PIN or the heat", ek(msgs(n0, "dev-lab")))
    check((msgs(n0, "dev-lab") or [{}])[0].get("note") is None,
          "…with no 'no bridge is reporting' note (the only watched bridge is not a fleet)")
    still = {e.kind: e.id for e in events("dev-lab") if e.resolved_at is None}
    check(still.get("pin_not_set") == before.get("pin_not_set") and still.get("temp_high") == before.get("temp_high"),
          "…and their episodes stay open (unknown is not resolved)", (before, still))
    check(all(e.clear_since is None for e in events("dev-lab") if e.kind in ("pin_not_set", "temp_high")),
          "…and not even counting down to a RESOLVED: an outage longer than a minute must not look like a fix",
          [(e.kind, e.clear_since) for e in events("dev-lab")])
    n0 = len(HOOK["got"])
    for _ in range(4):
        setdev("dev-lab"); tick()                             # back, same problems
    check(ek(msgs(n0, "dev-lab")) == [("resolved", "offline")],
          "it comes back: ONE email (offline resolved), the PIN and heat are not re-fired", ek(msgs(n0, "dev-lab")))
    after = {e.kind: e.id for e in events("dev-lab") if e.resolved_at is None}
    check(after == before, "the same PIN and heat episodes carry on (no duplicates in the history)", (before, after))
    drop("dev-lab")

    print("\nA reading at a threshold is one episode and one email")
    print("====================================================")
    bridge("dev-studio", 10, "Studio")
    n0 = len(HOOK["got"])
    for um in (14.6, 15.2, 14.8, 15.9, 14.1, 15.0, 14.9, 16.1, 14.3, 15.4):
        setdev("dev-studio", latest={"usb_misses_per_s": um, "pin": PIN_OK}); tick()
    got = ek(msgs(n0, "dev-studio"))
    check(got.count(("firing", "usb_misses")) == 1 and ("resolved", "usb_misses") not in got,
          "USB misses 14-16/s for 10 ticks: one FIRING, no RESOLVED (was 9 emails)", got)
    check(len(events("dev-studio", "usb_misses")) == 1, "…and one episode in the history", len(events("dev-studio", "usb_misses")))
    for _ in range(3):
        setdev("dev-studio", latest={"usb_misses_per_s": 13.0, "pin": PIN_OK}); tick()
    ev_ = events("dev-studio", "usb_misses")[-1]
    check(ev_.resolved_at is None and getattr(ev_, "clear_since", 0) is None and ("resolved", "usb_misses") not in ek(msgs(n0, "dev-studio")),
          "hysteresis: 13/s keeps an open USB alert open (it clears below 12, fires at 15)")
    for _ in range(4):
        setdev("dev-studio", latest={"usb_misses_per_s": 8.0, "pin": PIN_OK}); tick()
    got = ek(msgs(n0, "dev-studio"))
    check(got == [("firing", "usb_misses"), ("resolved", "usb_misses")], "clear for a minute: exactly one RESOLVED", got)
    bridge("dev-board", 11, "Board")
    n0 = len(HOOK["got"])
    for i in range(16):
        setdev("dev-board", latest={"power": {"live": i in (3, 11), "ok": False, "raw": "0x50005" if i in (3, 11) else "0x50000",
                                              "rate": {"pct": 1.0}}, "pin": PIN_OK}); tick()
    check(not msgs(n0, "dev-board"), "two one-tick brownout blips in 16 ticks: no email (was 4)", ek(msgs(n0, "dev-board")))
    check(len(events("dev-board", "throttled")) >= 1, "…but both are in the history (the audit trail stays)")
    bridge("dev-hot", 14, "Hot")
    n0 = len(HOOK["got"])
    for temp in ("80", "80", "80", "60", "60", "60", "60", "80", "80", "80", "80"):
        setdev("dev-hot", latest={"temp": temp + ".0'C", "pin": PIN_OK}); tick()
    got = ek(msgs(n0, "dev-hot"))
    check(got == [("firing", "temp_high"), ("resolved", "temp_high")],
          "hot, cool, hot again within 15 min: the second FIRING waits (one email per alert per 15 min)", got)
    for _ in range(26):
        setdev("dev-hot", latest={"temp": "80.0'C", "pin": PIN_OK}); tick()
    late = [m for m in msgs(n0, "dev-hot") if m.get("event") == "firing"]
    check(len(late) == 2 and late[1].get("repeats", 0) >= 1,
          "still hot after the 15 minutes: emailed then, saying it keeps coming back", [(m.get("event"), m.get("repeats")) for m in late])
    drop("dev-studio", "dev-board", "dev-hot")

    print("\nThe clear window: a one-pass gap is the same episode, RESOLVED waits a minute")
    print("=============================================================================")
    HOT, COOL = {"temp": "80.0'C", "pin": PIN_OK}, {"temp": "60.0'C", "pin": PIN_OK}
    bridge("dev-gap", 15, "Gap", latest=HOT)
    n0 = len(HOOK["got"])
    for _ in range(4):
        setdev("dev-gap", latest=HOT); tick()
    check(ek(msgs(n0, "dev-gap")) == [("firing", "temp_high")], "hot: emailed once", ek(msgs(n0, "dev-gap")))
    setdev("dev-gap", latest=COOL); tick()                        # cool for ONE pass (30 s)
    e1 = events("dev-gap", "temp_high")
    check(len(e1) == 1 and e1[0].resolved_at is None and e1[0].clear_since is not None
          and ek(msgs(n0, "dev-gap")) == [("firing", "temp_high")],
          "cool for 30 s: still open (counting down), no RESOLVED yet", [(e.resolved_at, e.clear_since) for e in e1])
    setdev("dev-gap", latest=HOT); tick()                         # hot again
    e2 = events("dev-gap", "temp_high")
    check(len(e2) == 1 and e2[0].resolved_at is None and e2[0].clear_since is None
          and ek(msgs(n0, "dev-gap")) == [("firing", "temp_high")],
          "hot again 30 s later: the SAME episode carries on - no RESOLVED, no second FIRING",
          (len(e2), ek(msgs(n0, "dev-gap"))))
    setdev("dev-gap", latest=COOL); tick()
    cleared = CLOCK[0]
    setdev("dev-gap", latest=COOL); tick()                        # clear for 30 s
    check(ek(msgs(n0, "dev-gap")) == [("firing", "temp_high")] and events("dev-gap", "temp_high")[0].resolved_at is None,
          "clear for 30 s: RESOLVED has not gone out yet", ek(msgs(n0, "dev-gap")))
    setdev("dev-gap", latest=COOL); tick()                        # clear for 60 s
    r_ = events("dev-gap", "temp_high")[0].resolved_at
    r_ = r_ and (r_ if r_.tzinfo else r_.replace(tzinfo=UTC))
    check(ek(msgs(n0, "dev-gap")) == [("firing", "temp_high"), ("resolved", "temp_high")] and r_ == cleared,
          "clear for a minute: exactly one RESOLVED, dated when it first cleared (not a minute later)", (ek(msgs(n0, "dev-gap")), r_, cleared))
    drop("dev-gap")

    print("\nThe fleet restarting does not page every bridge")
    print("===============================================")
    for i in range(1, 8):
        bridge("dev-r%d" % i, 30 + i, "Room %d" % i, seen=125)
    n0 = len(HOOK["got"])
    started = CLOCK[0]
    tick(0, started_at=started)
    rs = ["dev-r%d" % i for i in range(1, 8)]
    check(not any(events(d, "offline") for d in rs) and not msgs(n0),
          "first pass after a 2-minute fleet outage: no bridge is paged offline (was 7 CRITICAL emails)", ek(msgs(n0)))
    for d in rs[:6]:
        setdev(d, seen=-15)                                      # they report in
    tick(30, started_at=started)
    tick(59, started_at=started)                                 # 89 s after start: still inside the grace
    check(not any(events(d, "offline") for d in rs), "…nor while they report in (grace: OFFLINE_AFTER_S + two agent ticks)")
    for d in rs[:6]:
        setdev(d)
    tick(2, started_at=started)                                  # 91 s: Room 7 has had its chance
    tick(30, started_at=started)                                 # …and is still silent on the next pass
    check(ek(msgs(n0, "dev-r7")) == [("firing", "offline")] and not any(msgs(n0, d) for d in rs[:6]),
          "a bridge that stays silent past the grace IS paged, and only that one", ek(msgs(n0)))
    check(msgs(n0, "dev-r7") and msgs(n0, "dev-r7")[0].get("note") is None,
          "…as its own fault: 6 of 7 bridges report, so no 'no bridge is reporting' note")
    drop(*rs)
    import asyncio
    seen_kw, real_eval, real_sleep = {}, AL.evaluate, asyncio.sleep
    async def stop_after_first(_s):
        raise asyncio.CancelledError()
    AL.evaluate = lambda db_, **kw: seen_kw.update(kw) or {}
    asyncio.sleep = stop_after_first
    try:
        asyncio.run(AL.evaluate_loop(SessionLocal, 30))
    except BaseException:
        pass
    finally:
        AL.evaluate, asyncio.sleep = real_eval, real_sleep
    check(isinstance(seen_kw.get("started_at"), dt.datetime),
          "the running alert loop hands evaluate() the time the fleet started (the grace is wired in)", seen_kw)

    print("\nA reboot is not an outage")
    print("=========================")
    bridge("dev-boot", 27, "Boot")
    tick()
    n0 = len(HOOK["got"])
    setdev("dev-boot", seen=35); tick()                          # 65 s silent at this pass: offline, recorded
    for _ in range(4):
        setdev("dev-boot"); tick()                               # back: a brownout reset takes about a minute
    check(not msgs(n0, "dev-boot"), "a bridge silent for ~65 s (a reboot): no offline email and no RESOLVED (was both)",
          ek(msgs(n0, "dev-boot")))
    bo = events("dev-boot", "offline")
    check(len(bo) == 1 and bo[0].resolved_at is not None and bo[0].notified_at is None,
          "…but the blip is in the history, resolved and marked not emailed", [(e.resolved_at, e.notified_at) for e in bo])
    n1 = len(HOOK["got"])
    setdev("dev-boot", seen=35); tick(); tick()                  # silent 65 s, then 95 s: really gone
    check(ek(msgs(n1, "dev-boot")) == [("firing", "offline")], "silent on two passes (95 s): emailed", ek(msgs(n1, "dev-boot")))
    drop("dev-boot")

    print("\nMany at once is one email")
    print("=========================")
    ds = ["dev-d%d" % i for i in range(1, 6)]
    for i, d in enumerate(ds, 1):
        bridge(d, 40 + i, "Desk %d" % i)
    tick()
    p0, n0 = len(HOOK["got"]), len(HOOK["got"])
    for d in ds:
        setdev(d, seen=120)
    tick(0); tick()                                              # silent on two passes
    posts = HOOK["got"][p0:]
    check(len(posts) == 1 and posts[0].get("event") == "digest" and sorted(ek(msgs(n0))) == [("firing", "offline")] * 5,
          "5 bridges go silent in one pass: ONE digest with 5 offline items (was 5 emails)", [p.get("event") for p in posts])
    check(all(e.notified_at for d in ds for e in events(d, "offline")), "…and every episode is marked notified")
    check(posts and (posts[0].get("note") or "").startswith("No bridge in the fleet is reporting."),
          "no watched bridge is reporting: the digest says to check the venue's internet and the fleet's own address first",
          posts and posts[0].get("note"))
    p0 = len(HOOK["got"])
    for _ in range(3):
        for d in ds:
            setdev(d)
        tick()
    posts = HOOK["got"][p0:]
    check(len(posts) == 1 and posts[0].get("event") == "digest" and sorted(ek(msgs(p0))) == [("resolved", "offline")] * 5,
          "they come back: ONE digest with 5 resolved", [p.get("event") for p in posts])
    drop(*ds)

    print("\nA bridge forgotten while its email is going out")
    print("===============================================")
    gs = ["dev-g%d" % i for i in range(1, 6)]
    for i, d in enumerate(gs, 1):
        bridge(d, 60 + i, "Gone %d" % i)
    tick()
    for d in gs:
        setdev(d, seen=120)
    tick(0)                                                      # 5 offline episodes open (held one pass)
    real_deliver = NT.deliver
    def deliver_while_forgetting(payload, force=False):
        NT.deliver = real_deliver
        httpx.delete(BASE + "/admin/devices/dev-g3", headers=A)  # an admin forgets one bridge meanwhile
        return real_deliver(payload, force)
    NT.deliver = deliver_while_forgetting
    p0 = len(HOOK["got"])
    st = attempt(lambda: tick())
    NT.deliver = real_deliver
    check(isinstance(st, dict) and st.get("digests") == 1 and len(HOOK["got"]) - p0 == 1,
          "the pass completes and its digest is recorded as sent (was: StaleDataError, every mark rolled back)", st)
    check(all(e.notified_at for d in gs if d != "dev-g3" for e in events(d, "offline")),
          "…the four remaining bridges are marked notified")
    p1 = len(HOOK["got"]); tick()
    check(len(HOOK["got"]) == p1, "…so the next pass does not send that digest again",
          [p.get("event") for p in HOOK["got"][p1:]])
    drop(*gs)

    print("\nRestart storms: reboots with counters at 0, an odd counter, the panel")
    print("====================================================================")
    bridge("dev-brown", 15, "Brown")
    for i in range(12):                                          # a reset every 75 s, restarts 0/0/0 every time
        db.add(Telemetry(device_id="dev-brown", ts=CLOCK[0] - dt.timedelta(seconds=75 * (11 - i)),
                         metrics={"restarts": {"feeder_net": 0, "uvcd": 0, "return_audio": 0}, "boot_id": "boot-%d" % i}))
    bridge("dev-brown2", 16, "Brown old")
    for i in range(12):                                          # older image: no boot_id; `uptime -p` goes 0,1,0,1…
        db.add(Telemetry(device_id="dev-brown2", ts=CLOCK[0] - dt.timedelta(seconds=40 * (11 - i)),
                         metrics={"restarts": {"feeder_net": 0, "uvcd": 0, "return_audio": 0},
                                  "uptime": "0 minutes" if i % 2 == 0 else "1 minute"}))
    db.commit()
    def storm(id_):
        d = db.get(Device, id_)
        return attempt(lambda: AS.restart_storm(d, db, now=CLOCK[0] + dt.timedelta(seconds=1))) \
            if "now" in AS.restart_storm.__code__.co_varnames else AS.restart_storm(d, db)
    st = storm("dev-brown")
    check(isinstance(st, dict) and "reboots" in st.get("detail", ""), "11 reboots with every restart counter at 0 raise restart_storm", st)
    st = storm("dev-brown2")
    check(isinstance(st, dict) and "reboots" in st.get("detail", ""), "…and on an older bridge, from its uptime going down", st)
    SRC = ROOT / "pi/scripts/bridge-web.py"
    spec = importlib.util.spec_from_file_location("bw_boot", SRC); bw = importlib.util.module_from_spec(spec); spec.loader.exec_module(bw)
    bw.sh = lambda cmd: "active" if str(cmd).startswith("systemctl is-active") else ""
    (T / "boot_id").write_text("4b1c-boot\n")
    bw.BOOT_ID_FILE = str(T / "boot_id")
    g = attempt(bw.gather)
    check(isinstance(g, dict) and g.get("boot_id") == "4b1c-boot", "the bridge reports its boot_id in telemetry (read from the kernel)",
          g.get("boot_id") if isinstance(g, dict) else g)
    bridge("dev-odd", 17, "Odd", latest={"temp": "85.0'C", "pin": {"pin_set": True, "protocol": 2, "lockout": True, "lockout_remaining": 1800}})
    db.add(Telemetry(device_id="dev-odd", ts=CLOCK[0] - dt.timedelta(seconds=60), metrics={"restarts": {"uvcd": "n/a", "feeder_net": 0}}))
    db.add(Telemetry(device_id="dev-odd", ts=CLOCK[0] - dt.timedelta(seconds=30), metrics={"restarts": {"uvcd": 1, "feeder_net": 0}}))
    db.commit()
    n0 = len(HOOK["got"])
    for _ in range(4):
        setdev("dev-odd"); tick()
    check({"pin_lockout", "temp_high"} <= {k for _, k in ek(msgs(n0, "dev-odd"))},
          "one unreadable restart counter no longer silences the bridge: lockout and heat are emailed", ek(msgs(n0, "dev-odd")))
    bridge("dev-crash", 21, "Crash")
    for i, n in enumerate(range(0, 14, 2)):                      # bridge-uvcd crash-looping: 0, 2, 4 … 12
        db.add(Telemetry(device_id="dev-crash", ts=CLOCK[0] - dt.timedelta(seconds=30 * (6 - i)),
                         metrics={"restarts": {"feeder_net": 0, "uvcd": n, "return_audio": 0}, "boot_id": "b1"}))
    db.commit()
    setdev("dev-crash", seen=-3600); tick(0)                     # (seen in the future: online for the real-time server too)
    check([e.kind for e in events("dev-crash") if e.resolved_at is None] == ["restart_storm"], "the loop opens restart_storm for it")
    v = httpx.get(BASE + "/admin/devices/dev-crash", headers=A).json()
    kinds = [a["kind"] for a in v.get("alerts", [])]
    check(v.get("state") == "degraded" and "restart_storm" in kinds,
          "the panel shows it: 'Needs attention' with the restart storm (was 'Active', no alerts)", (v.get("state"), kinds))
    storm_a = [a for a in v.get("alerts", []) if a["kind"] == "restart_storm"]
    check(storm_a and storm_a[0].get("fix", {}).get("command") == "diagnose" and storm_a[0].get("severity") == "critical",
          "…with its fix (Collect diagnostics) and the email's severity", storm_a)
    al = httpx.get(BASE + "/admin/alerts", headers=A).json()
    br = httpx.get(BASE + "/admin/alerts/bridges", headers=A).json()
    row = [b for b in br["bridges"] if b["device_id"] == "dev-crash"]
    check(any(a["device_id"] == "dev-crash" and a["kind"] == "restart_storm" for a in al)
          and row and "restart_storm" in [a["kind"] for a in row[0]["open"]],
          "…on the Alerts page too (/admin/alerts and the bridge list)")
    lst = [d for d in httpx.get(BASE + "/admin/devices", headers=A).json() if d["id"] == "dev-crash"]
    check(lst and "restart_storm" in [a["kind"] for a in lst[0]["alerts"]], "…and in the fleet list")
    first = (storm_a[0].get("detail") if storm_a else "") or ""
    for i, n in enumerate((16, 20)):                             # it keeps crashing
        db.add(Telemetry(device_id="dev-crash", ts=CLOCK[0] + dt.timedelta(seconds=10 * (i + 1)),
                         metrics={"restarts": {"feeder_net": 0, "uvcd": n, "return_audio": 0}, "boot_id": "b1"}))
    db.commit()
    tick()
    v = httpx.get(BASE + "/admin/devices/dev-crash", headers=A).json()
    now_d = [a.get("detail") for a in v.get("alerts", []) if a["kind"] == "restart_storm"]
    check(first.startswith("12 service restarts") and now_d and now_d[0].startswith("20 service restarts"),
          "the panel's restart storm keeps its count current (12, then 20 restarts), not the first one it saw", (first, now_d))
    drop("dev-brown", "dev-brown2", "dev-odd", "dev-crash")

    print("\nSeveral failed services are one alert naming all of them")
    print("========================================================")
    bridge("dev-svc", 22, "Svc")
    three = [["bridge-feeder-net", "failed"], ["bridge-uvcd", "failed"], ["bridge-return-audio", "inactive"]]
    setdev("dev-svc", latest={"services": three, "pin": PIN_OK})
    sd = [a for a in AS.device_alerts(db.get(Device, "dev-svc")) if a["kind"] == "service_down"]
    check(len(sd) == 1 and all(n in sd[0]["detail"] for n in ("bridge-feeder-net", "bridge-uvcd", "bridge-return-audio")),
          "three units down: one service_down naming all three (one Restart button)", sd)
    n0 = len(HOOK["got"])
    for _ in range(3):
        setdev("dev-svc"); tick()
    m = [x for x in msgs(n0, "dev-svc") if x.get("kind") == "service_down"]
    check(len(m) == 1 and all(n in m[0]["detail"] for n in ("bridge-feeder-net", "bridge-uvcd", "bridge-return-audio")),
          "the email names all three (was only bridge-return-audio)", [x.get("detail") for x in m])
    setdev("dev-svc", latest={"services": three[:2] + [["bridge-return-audio", "active"]], "pin": PIN_OK}); tick()
    ev_ = events("dev-svc", "service_down")
    check(len(ev_) == 1 and "bridge-return-audio" not in (ev_[0].detail or "") and "bridge-uvcd" in (ev_[0].detail or ""),
          "one recovers: the open episode now names the two still down", [e.detail for e in ev_])
    drop("dev-svc")

    print("\nThe panel sees an open alert exactly as the alert loop does")
    print("===========================================================")
    bridge("dev-warm", 23, "Warm", latest={"temp": "80.0'C", "pin": PIN_OK})
    for _ in range(3):
        setdev("dev-warm"); tick()                               # Running hot: open and emailed
    setdev("dev-warm", latest={"temp": "73.0'C", "pin": PIN_OK}, seen=-3600); tick()
    ew = events("dev-warm", "temp_high")
    check(len(ew) == 1 and ew[0].resolved_at is None and getattr(ew[0], "clear_since", 0) is None,
          "73 °C: the loop keeps Running hot open and firing (it clears below 72 °C)",
          [(e.resolved_at, getattr(e, "clear_since", "n/a")) for e in ew])
    v = httpx.get(BASE + "/admin/devices/dev-warm", headers=A).json()
    warm = [a for a in v.get("alerts", []) if a["kind"] == "temp_high"]
    check(v.get("state") == "degraded" and warm and "clears below 72" in (warm[0].get("detail") or ""),
          "…and so does the panel, saying why at 73 °C: it used to call the bridge fine while no RESOLVED had been sent",
          (v.get("state"), warm))
    br = httpx.get(BASE + "/admin/alerts/bridges", headers=A).json()
    row = [b for b in br["bridges"] if b["device_id"] == "dev-warm"]
    check(row and "temp_high" in [a["kind"] for a in row[0]["open"]], "…on the Alerts page too")
    bridge("dev-mild", 24, "Mild", latest={"temp": "73.0'C", "pin": PIN_OK}, seen=-3600)
    v = httpx.get(BASE + "/admin/devices/dev-mild", headers=A).json()
    check(v.get("state") == "active" and not v.get("alerts"), "73 °C with no open alert: nothing (it fires at 75 °C)",
          (v.get("state"), v.get("alerts")))
    for _ in range(3):
        setdev("dev-warm", latest={"temp": "70.0'C", "pin": PIN_OK}, seen=-3600); tick()
    v = httpx.get(BASE + "/admin/devices/dev-warm", headers=A).json()
    check(events("dev-warm", "temp_high")[-1].resolved_at is not None and not v.get("alerts"),
          "under 72 °C for a minute: resolved in the loop and gone from the panel")
    drop("dev-warm", "dev-mild")

    print("\nOne odd reading does not silence a bridge")
    print("=========================================")
    weird = {"temp": "85.0'C", "power": {"live": False, "ok": True, "rate": {"pct": "3.5"}},
             "data_free_mb": float("-inf"), "usb_misses_per_s": float("nan"),
             "pin": {"pin_set": True, "protocol": 2, "lockout": True, "lockout_remaining": float("inf")}}
    bridge("dev-weird", 25, "Weird", latest=weird)
    wk = attempt(lambda: [a["kind"] for a in AS.device_alerts(db.get(Device, "dev-weird"))])
    check(isinstance(wk, list) and {"temp_high", "pin_lockout"} <= set(wk) and "telemetry_unreadable" not in wk,
          "a string where a number belongs, NaN and Infinity: the bridge's real alerts still come out", wk)
    n0 = len(HOOK["got"])
    for _ in range(3):
        setdev("dev-weird"); tick()
    got = {k for _, k in ek(msgs(n0, "dev-weird"))}
    check({"pin_lockout", "temp_high"} <= got, "…and are emailed (the loop used to skip the whole bridge)", got)
    drop("dev-weird")
    bridge("dev-garbled", 26, "Garbled", latest={"pin": {"pin_set": False, "required": True, "protocol": 2}})
    for _ in range(3):
        setdev("dev-garbled"); tick()                            # No PIN set: open and emailed
    real_da = AL.device_alerts
    def garbled(dev, *a, **k):
        if dev.id == "dev-garbled":
            raise ValueError("telemetry the fleet cannot read")
        return real_da(dev, *a, **k)
    AL.device_alerts = garbled
    n0 = len(HOOK["got"])
    try:
        for _ in range(4):
            setdev("dev-garbled"); tick()
    finally:
        AL.device_alerts = real_da
    got = ek(msgs(n0, "dev-garbled"))
    check(got == [("firing", "telemetry_unreadable")],
          "telemetry the loop cannot read is recorded and emailed as the panel shows it ('Unreadable status')", got)
    check([e.resolved_at for e in events("dev-garbled", "pin_not_set")] == [None],
          "…and the PIN alert is left open, not emailed RESOLVED (unknown is not fixed)")
    for _ in range(3):
        setdev("dev-garbled"); tick()
    check(ek(msgs(n0, "dev-garbled")) == [("firing", "telemetry_unreadable"), ("resolved", "telemetry_unreadable")],
          "readable again: 'Unreadable status' resolves, nothing else changes", ek(msgs(n0, "dev-garbled")))
    drop("dev-garbled")
    bridge("dev-back", 27, "Back", latest={"pin": {"pin_set": False, "required": True, "protocol": 2}})
    for _ in range(3):
        setdev("dev-back"); tick()                                # No PIN set: open and emailed
    n0 = len(HOOK["got"])
    for _ in range(3):
        setdev("dev-back", seen=120); tick()
    check(ek(msgs(n0, "dev-back")) == [("firing", "offline")], "it goes offline: emailed", ek(msgs(n0, "dev-back")))
    def garbled_back(dev, *a, **k):
        if dev.id == "dev-back":
            raise ValueError("telemetry the fleet cannot read")
        return real_da(dev, *a, **k)
    AL.device_alerts = garbled_back
    try:
        for _ in range(4):
            setdev("dev-back"); tick()                            # heartbeats again, telemetry garbled
    finally:
        AL.device_alerts = real_da
    got = ek(msgs(n0, "dev-back"))
    check(sorted(got) == [("firing", "offline"), ("firing", "telemetry_unreadable"), ("resolved", "offline")]
          and all(e.resolved_at is not None for e in events("dev-back", "offline")),
          "back online but its telemetry unreadable: the heartbeat still closes 'offline' and says RESOLVED", got)
    check([e.resolved_at for e in events("dev-back", "pin_not_set")] == [None],
          "…while the PIN alert, which it cannot see, stays open")
    drop("dev-back")

    print("\nA PIN lockout on older bridge software")
    print("======================================")
    bridge("dev-old", 12, "Old", latest={"pin": {"pin_set": True, "lockout": True, "lockout_remaining": 3000}})
    bridge("dev-new", 13, "New", latest={"pin": {"pin_set": True, "lockout": True, "lockout_remaining": 3000, "protocol": 2}})
    setdev("dev-old", seen=-3600); setdev("dev-new", seen=-3600)
    va = {d["id"]: d for d in httpx.get(BASE + "/admin/devices", headers=A).json()}
    lo = [a for a in va["dev-old"]["alerts"] if a["kind"] == "pin_lockout"]
    ln = [a for a in va["dev-new"]["alerts"] if a["kind"] == "pin_lockout"]
    check(lo and not lo[0]["fix"].get("command") and lo[0]["fix"].get("steps") and "cannot clear it remotely" in lo[0]["detail"],
          "2.1 bridge: no 'Clear the lockout' button the fleet would refuse (409); it says the lockout ends by itself", lo)
    check(ln and ln[0]["fix"].get("command") == "clear-lockout", "2.2 bridge: the one-click Clear the lockout stays", ln)
    r = httpx.post(BASE + "/admin/devices/dev-old/commands", headers=A, json={"type": "clear-lockout"})
    check(r.status_code == 409, "(the fleet does refuse clear-lockout there)", r.status_code)
    n0 = len(HOOK["got"])
    for _ in range(3):
        setdev("dev-old", seen=-3600); tick()
    mo = [x for x in msgs(n0, "dev-old") if x.get("kind") == "pin_lockout"]
    check(mo and (mo[0].get("fix") or {}).get("label") != "Clear the lockout" and not (mo[0].get("fix") or {}).get("command"),
          "…and its email does not advertise it", mo[:1])

    print("\nnb show prints alerts a person can read")
    print("=======================================")
    (T / "tok").write_text("ADMIN")
    out = subprocess.run([sys.executable, str(ROOT / "tools/nb"), "show", "NB-012"], capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, FLEET_URL=BASE, FLEET_TOKEN_FILE=str(T / "tok")))
    txt = out.stdout + out.stderr
    check(out.returncode == 0 and "{'kind'" not in txt and "PIN lockout:" in txt and "(fix: Wait for it to end)" in txt,
          "nb show: one line per alert with its fix, not a raw list of dicts", txt[-400:])
    out = subprocess.run([sys.executable, str(ROOT / "tools/nb"), "show", "NB-013"], capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, FLEET_URL=BASE, FLEET_TOKEN_FILE=str(T / "tok")))
    check("nb clear-lockout NB-013" in out.stdout, "…with the nb command for a one-click fix", out.stdout[-300:])
    drop("dev-old", "dev-new")

    print("\nWhat the emails say")
    print("===================")
    sent = []
    real_smtp = NT._smtp_send
    NT._smtp_send = lambda msg: sent.append(msg)
    S.smtp_host, S.alert_email_to, S.alert_email_from = "smtp.test", "owner@example.com", "fleet@example.com"
    o = CLOCK[0] - dt.timedelta(minutes=3); c = CLOCK[0] - dt.timedelta(minutes=1)
    def bm(*a, **k):
        try:
            return NT.build_message(*a, **k)
        except TypeError:
            return NT.build_message(*a)
    NT._send_email(bm("NB-009 · Lab", "dev-lab", "offline", "no heartbeat", "firing", AS.alert_fix("offline"), opened_at=o))
    NT._send_email(bm("NB-009 · Lab", "dev-lab", "offline", "no heartbeat", "resolved", None, opened_at=o, resolved_at=c))
    fire, res_ = (sent + [None, None])[:2]
    fb = fire.get_content() if fire else ""
    rb = res_.get_content() if res_ else ""
    check(res_ is not None and res_["Subject"] == "[NetBridge RESOLVED] NB-009 · Lab — Offline",
          "a recovery's subject leads with RESOLVED and carries no CRITICAL", res_ and res_["Subject"])
    check("Was    : no heartbeat" in rb and "Detail :" not in rb and "Cleared:" in rb and "(lasted 2 min)" in rb,
          "…and its body says what WAS wrong and when it cleared, not the firing detail again", rb)
    check("Since  :" in fb and "Open   : https://fleet.example/#/bridge/dev-lab" in fb and "Open   : https://fleet.example/#/bridge/dev-lab" in rb,
          "both carry the time and a link to the bridge in the panel", fb)
    t_ = attempt(lambda: NT._send_email(NT.build_test_message("admin@test")))
    check(t_ is True and sent[-1]["Subject"] == "[NetBridge TEST] Alert channel check from admin@test",
          "the test alert's subject says TEST and who sent it", sent[-1]["Subject"] if sent else t_)
    dg = attempt(lambda: NT.build_digest([bm("NB-%03d · D" % i, "d%d" % i, "offline", "no heartbeat", "firing", AS.alert_fix("offline"))
                                          for i in range(1, 6)]))
    n_before = len(sent)
    attempt(lambda: NT._send_email(dg))
    db_ = sent[-1].get_content() if len(sent) > n_before else ""
    check(len(sent) > n_before and sent[-1]["Subject"] == "[NetBridge CRITICAL] 5 alerts at once — Offline"
          and db_.count("1. Check it has power") == 1 and db_.count("FIRING: Offline") == 5,
          "a digest: one subject for 5 bridges, each listed, the fix steps once", sent[-1]["Subject"] if len(sent) > n_before else dg)
    three = [bm("NB-%03d · %s" % (n, nm), "d-" + nm, "offline", "no heartbeat", "firing",
                AS.alert_fix("offline", types.SimpleNamespace(pairing_code="BRIDGE-" + code)))
             for n, nm, code in ((1, "Hall", "AAA1"), (2, "Lab", "BBB2"), (3, "Gym", "CCC3"))]
    n_before = len(sent)
    attempt(lambda: NT._send_email(NT.build_digest(three)))
    tb = sent[-1].get_content() if len(sent) > n_before else ""
    check(all(tb.count("setup Wi-Fi BridgeSetup-" + c) == 1 for c in ("AAA1", "BBB2", "CCC3")),
          "a digest of bridges whose fixes differ gives each its OWN steps (its own setup Wi-Fi), not the first one's", tb[:900])
    fmt0 = S.alert_webhook_format
    many = [bm("NB-%03d · Venue %d" % (i, i), "d%d" % i, "offline", "no heartbeat", "firing", AS.alert_fix("offline"))
            for i in range(1, 61)]
    for fmt, key, hard in (("discord", "content", 2000), ("slack", "text", 4000)):
        S.alert_webhook_format = fmt
        body = attempt(lambda: NT._webhook_body(NT.build_digest(many)))
        txt = body.get(key, "") if isinstance(body, dict) else ""
        rows = txt.split("\n")[1:]
        m = re.fullmatch(r"…and (\d+) more — open the fleet panel's Alerts page", rows[-1] if rows else "")
        kept = rows[:-1]
        check(0 < len(txt) <= hard and m and len(kept) + int(m.group(1)) == 60
              and kept == [NT._summary_line(p) for p in many[:len(kept)]],
              "%s: a 60-bridge digest fits one message (%d chars), whole lines, '…and %s more'"
              % (fmt, len(txt), m.group(1) if m else "?"), txt[-300:])
        small = attempt(lambda: NT._webhook_body(NT.build_digest(many[:3])))
        st_ = small.get(key, "") if isinstance(small, dict) else ""
        check(st_.count("\n") == 3 and "more —" not in st_, "%s: a short digest is sent whole" % fmt, st_)
    S.alert_webhook_format = fmt0
    line = NT._summary_line(bm("NB-001 · Hall", "dev-a", "pin_not_set", "no PIN", "firing", AS.alert_fix("pin_not_set")))
    check("No PIN set" in line and "pin_not_set" not in line, "chat messages use the readable title, not the slug", line)
    line = NT._summary_line(bm("NB-009 · Lab", "dev-lab", "offline", "no heartbeat", "resolved", None, opened_at=o, resolved_at=c))
    check(line.startswith("🟢 RESOLVED · Offline · NB-009 · Lab — was: no heartbeat"),
          "a chat recovery says what WAS wrong, like the email ('RESOLVED … — no heartbeat' read as a contradiction)", line)
    silent = []
    for i in range(1, 6):
        p = bm("NB-%03d · D" % i, "d%d" % i, "offline", "no heartbeat", "firing", AS.alert_fix("offline"))
        p["note"] = getattr(AL, "FLEET_SILENT_NOTE", "(none)")
        silent.append(p)
    n_before = len(sent)
    attempt(lambda: NT._send_email(NT.build_digest(silent)))
    sb = sent[-1].get_content() if len(sent) > n_before else ""
    check(len(sent) > n_before and sent[-1]["Subject"] == "[NetBridge CRITICAL] No bridge is reporting — 5 offline at once"
          and sb.startswith("No bridge in the fleet is reporting.") and sb.count("No bridge in the fleet is reporting") == 1,
          "every watched bridge silent: 'No bridge is reporting', and what to check first, said once",
          (sent[-1]["Subject"] if len(sent) > n_before else None, sb[:200]))
    # A refused recipient, or a login error, must not put an address or the app password on the panel.
    hook_url, S.alert_webhook_url = S.alert_webhook_url, ""     # email only for these two
    S.alert_email_to, S.smtp_user, S.smtp_password = "owner@example.com, ops@example.org", "fleet@example.com", "s3cret-app-pw"
    NT._smtp_send = lambda msg: (_ for _ in ()).throw(smtplib.SMTPRecipientsRefused({"owner@example.com": (550, b"5.1.1 no such user")}))
    def email_status():
        cs = attempt(lambda: NT.channel_status())
        return (cs.get("email") or {}) if isinstance(cs, dict) else {}
    attempt(lambda: NT.deliver(bm("NB-009 · Lab", "dev-lab", "offline", "no heartbeat", "firing", AS.alert_fix("offline")), force=True))
    e1 = email_status().get("last_error") or ""
    NT._smtp_send = lambda msg: (_ for _ in ()).throw(RuntimeError("login refused for fleet@example.com with s3cret-app-pw"))
    attempt(lambda: NT.deliver(bm("NB-009 · Lab", "dev-lab", "offline", "no heartbeat", "firing", AS.alert_fix("offline")), force=True))
    cs_ = email_status()
    e2 = cs_.get("last_error") or ""
    check("SMTPRecipientsRefused" in e1 and "owner@example.com" not in e1 and "ow…@example.com" in e1,
          "an SMTP refusal on the panel masks the recipient it quotes", e1)
    check("s3cret-app-pw" not in e2 and "fleet@example.com" not in e2 and "<password>" in e2,
          "…and a login error never shows the app password or the full login", e2)
    check(cs_.get("target") == "ow…@example.com, op…@example.org", "several recipients are masked one by one", cs_.get("target"))
    attempt(lambda: NT._health["email"].update(fails=0, failing_since=None, last_error=None, next_try=0.0))
    S.smtp_password, S.alert_webhook_url = "", hook_url
    NT._smtp_send = real_smtp
    calls = []
    real_SMTP = smtplib.SMTP
    smtplib.SMTP = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("connected"))
    S.smtp_user, S.smtp_password = "owner@example.com", ""
    e = attempt(lambda: NT._smtp_send(NT.EmailMessage()))
    smtplib.SMTP = real_SMTP
    check(not calls and isinstance(e, Exception) and "SMTP_PASSWORD" in str(e),
          "SMTP_USER set with an empty SMTP_PASSWORD: no login is attempted, the error says why", (calls, e))
    cs = attempt(lambda: NT.channel_status())
    check(isinstance(cs, dict) and "SMTP_PASSWORD" in (cs.get("email", {}).get("warning") or ""),
          "…and the channel status warns about it", cs)
    S.smtp_host = S.alert_email_to = S.alert_email_from = S.smtp_user = ""

    print("\nFailed delivery backs off instead of retrying every alert every tick")
    print("===================================================================")
    fs = ["dev-f%d" % i for i in range(1, 7)]
    for i, d in enumerate(fs, 1):
        bridge(d, 50 + i, "Floor %d" % i, latest={"temp": "80.0'C", "pin": PIN_OK})
    HOOK["status"] = 500
    t0 = HOOK["tries"]
    for _ in range(6):
        for d in fs:
            setdev(d)
        tick()
    tries = HOOK["tries"] - t0
    check(tries <= 2, "6 open alerts, webhook failing, 6 passes: %d delivery attempts (was one per alert per pass)" % tries)
    check(not any(e.notified_at for d in fs for e in events(d, "temp_high")), "…nothing is marked notified while it fails")
    cs = attempt(lambda: NT.channel_status())
    w = cs.get("webhook", {}) if isinstance(cs, dict) else {}
    check(w.get("failing") and w.get("next_try_at") and "500" in (w.get("last_error") or "") and "hook-secret-path" not in json.dumps(cs),
          "the channel reports failing, since when, the error and the next try - never the webhook URL", cs)
    HOOK["status"] = 200
    n0 = len(HOOK["got"])
    for _ in range(8):
        for d in fs:
            setdev(d)
        tick()
    check(sorted(ek(msgs(n0))) == [("firing", "temp_high")] * 6 and all(e.notified_at for d in fs for e in events(d, "temp_high")),
          "once the channel works again the held alerts go out (and are marked notified)", ek(msgs(n0)))
    HOOK["status"] = 500
    gaps = []
    for _ in range(8):
        CLOCK[0] += dt.timedelta(hours=1)                         # well past any back-off
        attempt(lambda: NT.deliver(bm("NB-051 · Floor 1", "dev-f1", "temp_high", "80.0'C", "firing", AS.alert_fix("temp_high"))))
        gaps.append(round(NT._health["webhook"]["next_try"] - NT._now()))
    HOOK["status"] = 200
    check(gaps == [30, 120, 600, 600, 600, 600, 600, 600],
          "the wait grows 30 s, 2 min, 10 min and stops there: a channel that recovers is used again within 10 min (was 1 h)", gaps)
    attempt(lambda: NT._health["webhook"].update(fails=0, failing_since=None, last_error=None, next_try=0.0))
    drop(*fs)

    print("\nThe panel and the test alert say where alerts go")
    print("================================================")
    HOOK["status"] = 500
    r = httpx.post(BASE + "/admin/alerts/test", headers=A)
    ch = httpx.get(BASE + "/admin/alerts/channels", headers=A)
    cj = ch.json() if ch.status_code == 200 else {}
    check(r.status_code == 200 and not r.json().get("ok") and cj.get("webhook", {}).get("failing"),
          "a failing webhook shows as failing on /admin/alerts/channels", (r.text[:200], ch.status_code, cj))
    check(cj.get("webhook", {}).get("target") == "127.0.0.1" and "hook-secret-path" not in ch.text and cj.get("email", {}).get("configured") is False,
          "…naming only the webhook's host (its URL can be a secret), and email as not set up", cj)
    db.add(User(email="admin@acme.test", role="admin", org_id="acme", token_hash=hash_token("ACME"))); db.commit()
    oc = httpx.get(BASE + "/admin/alerts/channels", headers={"Authorization": "Bearer ACME"})
    oj = oc.json() if oc.status_code == 200 else {}
    check(oc.status_code == 200 and oj == {"any": True, "managed": True, "email": {"configured": False}, "webhook": {"configured": True}},
          "another organisation's admin learns only that alerts are delivered - not where to, not that a channel is failing",
          (oc.status_code, oc.text[:300]))
    check(httpx.get(BASE + "/admin/alerts/channels", headers={"Authorization": "Bearer PRES"}).status_code == 401
          and httpx.get(BASE + "/admin/alerts/channels").status_code == 401, "/admin/alerts/channels is admin-only")
    HOOK["status"] = 200
    n0 = len(HOOK["got"])
    r = httpx.post(BASE + "/admin/alerts/test", headers=A)
    tm = HOOK["got"][n0:]
    check(r.status_code == 200 and r.json().get("ok") and tm and tm[0].get("event") == "test" and tm[0].get("severity") == "info"
          and tm[0].get("kind") == "test" and tm[0].get("sent_by") == "admin@test",
          "'Send a test alert' is a TEST message from the admin, not a CRITICAL 'offline' page", tm[:1])
    check(httpx.get(BASE + "/admin/alerts/channels", headers=A).json().get("webhook", {}).get("failing") is False,
          "…and a successful test clears the failing state")
    panel = (ROOT / "control-plane/panel-dist/index.html").read_text()
    check("Alerts are also emailed.</p>" not in panel and "/admin/alerts/channels" in panel and "No alert channel is set up" in panel,
          "the Alerts page words where alerts go from the fleet (no fixed 'Alerts are also emailed')")
    check("<th>Notified</th>" in panel and "<th>Emailed</th>" not in panel, "the history column says Notified (a webhook counts too)")
    check("c.managed" in panel and "the fleet operator's alert channels" in panel,
          "…and for another organisation it says alerts go through the operator's channels, naming none")
    check("has been failing since " in panel and "is failing\" + (x.failing_since" not in panel,
          "a failing channel reads 'has been failing since 10:15', not 'is failing since 10:15'")
    check("um >= 15" not in panel and ">= 75 ?" not in panel and "< 500 ?" not in panel and 'hasAlert(d, "temp_high")' in panel,
          "panel colours follow the fleet's alerts, not copies of its thresholds")

    print("\nA fleet database from before storm control upgrades in place")
    print("============================================================")
    import sqlite3
    OLD = T / "old-fleet.db"
    con = sqlite3.connect(OLD)
    con.execute("CREATE TABLE alert_events (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id VARCHAR, kind VARCHAR, "
                "detail VARCHAR, opened_at DATETIME, notified_at DATETIME, resolved_at DATETIME, resolve_notified_at DATETIME)")
    con.execute("INSERT INTO alert_events (device_id, kind, detail, opened_at, notified_at) VALUES "
                "('dev-old', 'offline', 'no heartbeat', '2026-09-27 10:00:00.000000', '2026-09-27 10:00:00.000000')")
    con.commit(); con.close()
    probe = ("import app.main\nfrom app.db import SessionLocal\nfrom app.models import AlertEvent\n"
             "e = SessionLocal().query(AlertEvent).one()\nprint(e.kind, e.detail, getattr(e, 'clear_since', 'n/a'))")
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(BACKEND), capture_output=True, text=True, timeout=120,
                       env=dict(ENV, DATABASE_URL="sqlite:///%s" % OLD))
    con = sqlite3.connect(OLD)
    cols = [row[1] for row in con.execute("PRAGMA table_info(alert_events)")]
    idx = [row[1] for row in con.execute("PRAGMA index_list(alert_events)")]
    con.close()
    check(r.returncode == 0 and "clear_since" in cols and r.stdout.strip().endswith("offline no heartbeat None"),
          "the alert_events table of an older fleet gains clear_since, and its episodes read as they were",
          (r.returncode, cols, r.stdout[-200:], r.stderr[-300:]))
    check("ix_alert_events_device_resolved" in idx, "…and the (device, resolved) index", idx)
    con = sqlite3.connect(T / "fleet.db")
    plan = " ".join(str(row[-1]) for row in con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM alert_events WHERE device_id IN ('a', 'b') AND resolved_at IS NULL"))
    con.close()
    check("ix_alert_events_device_resolved" in plan,
          "the panel's once-a-second 'open episodes' read uses it, instead of every episode a bridge ever had", plan)
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    _hook.shutdown()

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
