#!/usr/bin/env python3
"""The fleet server, run for real (2026-09-24): who may see and do what, bridge numbers and
states, the new alerts, the owner's never-LAN rule, command lifecycle, and the live stream.

Starts the actual control plane (uvicorn) on a random local port against a throwaway SQLite
database, and talks to it over HTTP exactly as the panel, the presenter app and a bridge do.
Needs FastAPI: uses ~/netbridge/fleet-test-venv when the system Python lacks it; skips (and says
so) when neither has it."""
import os, pathlib, sys

try:
    import fastapi, httpx, uvicorn  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIP  FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import datetime as dt, json, socket, subprocess, tempfile, threading, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "control-plane/backend"
T = pathlib.Path(tempfile.mkdtemp())
DB = T / "fleet.db"
ENV = dict(os.environ, DATABASE_URL="sqlite:///%s" % DB, BOOTSTRAP_TOKENS="boot-test",
           PAYLOAD_DIR=str(T / "payloads"), OFFLINE_AFTER_S="60")
os.environ.update(ENV)
sys.path.insert(0, str(BACKEND))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:300]) if detail else ""))

# ---- schema + accounts, through the app's own models ------------------------------------------
from app import main as M                       # creates tables, migrates, backfills
from app.models import User, Device
from app.auth import hash_token
db = M.SessionLocal() if hasattr(M, "SessionLocal") else None
from app.db import SessionLocal
db = SessionLocal()
db.add(User(email="admin@test", role="admin", org_id="default", token_hash=hash_token("ADMIN")))
db.add(User(email="presenter@test", role="presenter", org_id="default", token_hash=hash_token("PRES")))
db.commit()

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
P = {"Authorization": "Bearer PRES"}
c = httpx.Client(base_url=BASE, timeout=10)

try:
    # ---- bridges join and report --------------------------------------------------------------
    dev_tok = {}
    for i, serial in enumerate(("10000000aaaa0001", "10000000aaaa0002", "10000000aaaa0003", "10000000aaaa0004")):
        r = c.post("/v1/enroll", json={"bootstrap_token": "boot-test", "device_id": serial,
                                       "pairing_code": "BRIDGE-%04d" % (i + 1), "version": "2.1.0-test",
                                       "tailscale_ip": "100.64.0.%d" % (i + 10), "hostname": "nb-%d" % i})
        dev_tok[serial] = {"Authorization": "Bearer " + r.json()["device_token"]}
    ids = list(dev_tok)
    def tel(serial, **kw):
        body = {"version": "2.1.0-test", "ip": "192.168.1.%d" % (ids.index(serial) + 50), "udc": "not attached",
                "streams": {"video": False, "voice": False, "return": False},
                "pin": {"pin_set": True, "locked": True, "lockout": False, "required": True}}
        body.update(kw)
        return c.post("/v1/telemetry", headers=dev_tok[serial], json=body)
    for sid in ids:
        tel(sid)

    # ---- claim -> numbers in claim order --------------------------------------------------------
    for sid, name in ((ids[1], "Studio B"), (ids[0], "Studio A"), (ids[2], "Hall")):
        r = c.post("/admin/devices/%s/claim" % sid, headers=A, json={"name": name})
        check(r.status_code == 200, "admin claims %s" % name, r.text)
    view = {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}
    check([view[ids[1]]["label"], view[ids[0]]["label"], view[ids[2]]["label"]] == ["NB-001", "NB-002", "NB-003"],
          "claimed bridges are numbered in claim order: NB-001, NB-002, NB-003",
          [view[i]["label"] for i in ids])
    check(view[ids[3]]["number"] is None and view[ids[3]]["state"] == "new",
          "an unclaimed bridge has no number and shows as NEW")

    # ---- backfill for fleets older than numbering -------------------------------------------------
    for sid in ids[:3]:
        db.get(Device, sid).number = None
    db.commit()
    M._backfill_numbers()
    db.expire_all()
    nums = [db.get(Device, sid).number for sid in (ids[1], ids[0], ids[2])]
    check(nums == [1, 2, 3], "existing bridges are backfilled in the order they were claimed", nums)

    # ---- rename / renumber -----------------------------------------------------------------------
    r = c.patch("/admin/devices/%s" % ids[2], headers=A, json={"name": "Main Hall", "number": 7})
    check(r.status_code == 200 and r.json()["label"] == "NB-007" and r.json()["name"] == "Main Hall",
          "admin renames and renumbers a bridge (NB-007 Main Hall)", r.text)
    r = c.patch("/admin/devices/%s" % ids[2], headers=A, json={"number": 1})
    check(r.status_code == 409, "a number already in use is refused (409)", r.text)
    r = c.patch("/admin/devices/%s" % ids[2], headers=A, json={"name": "  "})
    check(r.status_code == 400, "an empty name is refused")
    r = c.patch("/admin/devices/%s" % ids[2], headers=P, json={"name": "hijack"})
    check(r.status_code == 401, "a presenter cannot rename a bridge")

    # ---- who sees what ---------------------------------------------------------------------------
    for path in ("/admin/devices", "/admin/devices/%s" % ids[0], "/admin/alerts",
                 "/admin/devices/%s/uptime" % ids[0], "/admin/rollouts", "/admin/devices/%s/commands" % ids[0],
                 "/admin/audit", "/admin/users", "/admin/payloads"):
        r = c.get(path, headers=P)
        check(r.status_code == 401, "presenter refused: GET %s" % path, r.status_code)
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=P, json={"type": "restart"})
    check(r.status_code == 401, "presenter cannot send a command")
    r = c.get("/auth/bridges", headers=P)
    b = r.json() if r.status_code == 200 else []
    check(r.status_code == 200 and [x["label"] for x in b] == ["NB-001", "NB-002", "NB-007"],
          "presenter gets the claimed bridges, by number, to go live", r.text)
    check(b and set(b[0]) == {"id", "number", "label", "name", "pairing_code", "online", "tailscale_ip", "ip"},
          "…with only what routing needs — no live telemetry, alerts or history", b[:1])
    r = c.get("/auth/bridges", headers=A)
    check(r.status_code == 200 and len(r.json()) == 3, "an admin who presents gets the same list")
    check(c.get("/auth/whoami", headers=P).json().get("role") == "presenter", "whoami still answers presenters")
    check(c.get("/auth/bridges").status_code == 401, "no token, no bridges")

    # ---- states and alerts -----------------------------------------------------------------------
    tel(ids[1], streams={"video": True, "voice": True, "return": True}, udc="configured",
        mesh_path={"via": "relay"})
    tel(ids[0])
    tel(ids[2], usb_misses_per_s=31.5, data_free_mb=120, quarantined=["bridge-web.py"],
        overrides={"safe_mode": True, "pending": [], "active": []},
        ota={"state": "rolled back", "version": "2.1.1-abc1234", "detail": "unhealthy", "ts": time.time()},
        pin={"pin_set": False, "required": True})
    view = {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}
    check(view[ids[1]]["state"] == "live" and view[ids[1]]["laptop"] is True,
          "NB-001 is LIVE, laptop attached")
    check(view[ids[0]]["state"] == "active" and view[ids[0]]["laptop"] is False,
          "NB-002 is ACTIVE (online, healthy, idle)")
    kinds = {a["kind"] for a in view[ids[2]]["alerts"]}
    check(view[ids[2]]["state"] == "degraded", "NB-007 is DEGRADED (online with alerts)")
    for k in ("usb_misses", "disk_low", "update_rolled_back", "safe_mode", "os_update_failed", "pin_not_set"):
        check(k in kinds, "alert: %s" % k, kinds)
    check(any("BLOCKED" in a["detail"] for a in view[ids[2]]["alerts"] if a["kind"] == "pin_not_set"),
          "no-PIN alert says go-live is blocked on the new image")
    check("mesh_relayed" in {a["kind"] for a in view[ids[1]]["alerts"]}, "alert: a live session is relayed")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    db.get(Device, ids[0]).last_seen = old; db.commit()
    view = {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}
    check(view[ids[0]]["state"] == "offline" and view[ids[0]]["laptop"] is None,
          "no heartbeat for 10 min -> OFFLINE")
    tel(ids[0])

    # ---- commands --------------------------------------------------------------------------------
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "profile", "args": {"mode": "lan"}})
    check(r.status_code == 400 and "LAN" in r.text, "the LAN profile is refused by the fleet (owner's rule)", r.text)
    r = c.post("/admin/commands/broadcast", headers=A, json={"type": "profile", "args": {"mode": "LAN"}})
    check(r.status_code == 400, "…also when broadcast to every bridge")
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "reboot"})
    check(r.status_code == 400, "a reboot without confirmation is refused")
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "reboot", "confirm": True})
    rid = r.json().get("id")
    check(r.status_code == 200 and rid, "a confirmed reboot is queued", r.text)
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "set-pin", "args": {"pin": "123456"}})
    pid = r.json().get("id")
    hist = c.get("/admin/devices/%s/commands" % ids[0], headers=A).json()
    check(all("pin" not in (h.get("args") or {}) for h in hist), "a queued PIN is never shown back to a browser")
    pulled = c.get("/v1/commands", headers=dev_tok[ids[0]]).json()
    check({p["id"] for p in pulled} == {rid, pid} and any(p.get("args", {}).get("pin") == "123456" for p in pulled),
          "the bridge (and only the bridge) receives the command with its PIN")
    c.post("/v1/commands/%s/result" % rid, headers=dev_tok[ids[0]], json={"status": "done", "output": "rebooting"})
    st = {h["id"]: h["status"] for h in c.get("/admin/devices/%s/commands" % ids[0], headers=A).json()}
    check(st.get(rid) == "done", "the result comes back to the fleet")

    # ---- live stream -----------------------------------------------------------------------------
    r = httpx.get(BASE + "/admin/stream", headers=P, timeout=5)
    check(r.status_code == 401, "presenter cannot open the live stream")
    got, stop = [], threading.Event()
    def reader():
        with httpx.stream("GET", BASE + "/admin/stream", headers=A, timeout=30) as resp:
            got.append(("status", resp.status_code, resp.headers.get("content-type")))
            ev = None
            for line in resp.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: ") and ev:
                    got.append((ev, json.loads(line[6:]), time.monotonic()))
                    ev = None
    th = threading.Thread(target=reader, daemon=True); th.start()
    deadline = time.time() + 10
    while time.time() < deadline and not any(g[0] == "ready" for g in got):
        time.sleep(0.1)
    check(got and got[0][1] == 200 and "text/event-stream" in (got[0][2] or ""), "admin opens the live stream", got[:1])
    first = [g for g in got if g[0] == "device"]
    check(any(g[0] == "hello" for g in got) and len({g[1]["id"] for g in first}) == 4,
          "the stream starts with every bridge (hello, 4 devices, ready)", len(first))
    n_before = len(got)
    t0 = time.monotonic()
    tel(ids[0], streams={"video": True, "voice": True, "return": False})
    while time.monotonic() - t0 < 5 and not any(g[0] == "device" and g[1]["id"] == ids[0] and g[1]["state"] == "live"
                                                  for g in got[n_before:]):
        time.sleep(0.05)
    hit = [g for g in got[n_before:] if g[0] == "device" and g[1]["id"] == ids[0] and g[1]["state"] == "live"]
    check(bool(hit), "a bridge going live is pushed to the page", [g[0] for g in got[n_before:]])
    if hit:
        check(hit[0][2] - t0 < 2.5, "…within %.1f s (pushed, not polled every 5 s)" % (hit[0][2] - t0))
    others = [g for g in got[n_before:] if g[0] == "device" and g[1]["id"] != ids[0]]
    check(not others, "…and only that bridge is re-sent (the page is not rebuilt)", [o[1]["id"] for o in others])
    n_before = len(got)
    r = c.post("/admin/devices/%s/commands" % ids[1], headers=A, json={"type": "running"})
    cid = r.json()["id"]
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5 and not any(g[0] == "command" and g[1]["id"] == cid for g in got[n_before:]):
        time.sleep(0.05)
    check(any(g[0] == "command" and g[1]["id"] == cid and g[1]["status"] == "pending" for g in got[n_before:]),
          "a queued command appears live with its status")
    c.get("/v1/commands", headers=dev_tok[ids[1]])
    c.post("/v1/commands/%s/result" % cid, headers=dev_tok[ids[1]], json={"status": "done", "output": "all built-in"})
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5 and not any(g[0] == "command" and g[1]["id"] == cid and g[1]["status"] == "done"
                                                  for g in got):
        time.sleep(0.05)
    fin = [g for g in got if g[0] == "command" and g[1]["id"] == cid and g[1]["status"] == "done"]
    check(bool(fin) and fin[-1][1]["output"] == "all built-in", "…and its result and output arrive live too")
    stop.set()
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
