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
           PAYLOAD_DIR=str(T / "payloads"), OFFLINE_AFTER_S="60", ALERT_EVAL_INTERVAL_S="3600")
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
                "pin": {"pin_set": True, "locked": True, "lockout": False, "required": True, "protocol": 2}}
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
    # claimed once (2026-09-28): a second claim staged a new mesh key and reset the bridge's mesh
    r = c.post("/admin/devices/%s/claim" % ids[1], headers=A, json={"name": "Renamed by claim"})
    check(r.status_code == 409 and "already claimed" in r.text, "claiming a claimed bridge again is refused (409)", r.text)
    check({d["id"]: d for d in c.get("/admin/devices", headers=A).json()}[ids[1]]["name"] == "Studio B",
          "…and its name is unchanged")
    r = c.post("/admin/devices/%s/claim" % ids[3], headers=A, json={"name": ""})
    check(r.status_code == 400, "claim refuses an empty name, like rename does", r.text)

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
    # revoking ends the sign-in, and it never becomes the next account's (2026-09-28)
    r = c.post("/admin/users", headers=A, json={"email": "temp@example.test", "role": "presenter"})
    ttok = c.post("/auth/redeem", json={"invite": r.json().get("invite", "")}).json().get("token", "")
    TT = {"Authorization": "Bearer " + ttok}
    check(c.get("/auth/whoami", headers=TT).status_code == 200, "an invited presenter signs in")
    tid = next(u["id"] for u in c.get("/admin/users", headers=A).json() if u["email"] == "temp@example.test")
    c.delete("/admin/users/%d" % tid, headers=A)
    check(c.get("/auth/whoami", headers=TT).status_code == 401, "…revoked, their token is refused")
    c.post("/admin/users", headers=A, json={"email": "next-admin@example.test", "role": "admin"})
    r = c.get("/auth/whoami", headers=TT)
    check(r.status_code == 401 and c.get("/admin/devices", headers=TT).status_code == 401,
          "…and it does not come back as the admin added next", r.text)

    # ---- states and alerts -----------------------------------------------------------------------
    tel(ids[1], streams={"video": True, "voice": True, "return": True}, udc="configured",
        mesh_path={"via": "relay"})
    tel(ids[0])
    tel(ids[2], usb_misses_per_s=31.5, data_free_mb=120, quarantined=["bridge-web.py"],
        overrides={"safe_mode": True, "pending": [], "active": []},
        ota={"state": "rolled back", "version": "2.1.1-abc1234", "detail": "unhealthy", "ts": time.time()},
        pin={"pin_set": False, "required": True, "protocol": 2})
    view = {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}
    check(view[ids[1]]["state"] == "live" and view[ids[1]]["laptop"] is True,
          "NB-001 is LIVE, laptop attached")
    check(view[ids[0]]["state"] == "active" and view[ids[0]]["laptop"] is False,
          "NB-002 is ACTIVE (online, healthy, idle)")
    kinds = {a["kind"] for a in view[ids[2]]["alerts"]}
    check(view[ids[2]]["state"] == "degraded", "NB-007 is DEGRADED (online with alerts)")
    for k in ("usb_misses", "disk_low", "update_rolled_back", "safe_mode", "os_update_failed", "pin_not_set"):
        check(k in kinds, "alert: %s" % k, kinds)
    det = [a["detail"] for a in view[ids[2]]["alerts"] if a["kind"] == "os_update_failed"]
    check(det == ["OS update 2.1.1-abc1234 rolled back — unhealthy"], "OS update alert text: version, state, reason", det)
    check(any("BLOCKED" in a["detail"] for a in view[ids[2]]["alerts"] if a["kind"] == "pin_not_set"),
          "no-PIN alert says go-live is blocked on the new image")
    check("mesh_relayed" in {a["kind"] for a in view[ids[1]]["alerts"]}, "alert: a live session is relayed")
    # An update refused before its manifest was read has no version (2026-09-28: "OS update  failed").
    tel(ids[2], ota={"state": "failed", "version": "", "ts": time.time(),
                     "detail": "the meeting laptop is attached — try again after the meeting"})
    det = [a["detail"] for a in {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}[ids[2]]["alerts"]
           if a["kind"] == "os_update_failed"]
    check(det == ["OS update failed — the meeting laptop is attached — try again after the meeting"],
          "an OS update that failed before it had a version reads cleanly (no double space)", det)
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
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "unlock", "args": {"pin": "123456"}})
    check(r.status_code == 400, "remote unlock no longer exists (only a presenter typing the PIN opens a session)", r.text)
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "clear-lockout"})
    check(r.status_code == 200, "clear-lockout is accepted", r.text)
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "lock"})
    check(r.status_code == 400, "lock (ends the live session) needs confirmation", r.text)
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "lock", "confirm": True})
    check(r.status_code == 200, "…and is queued once confirmed", r.text)
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
    check({rid, pid} <= {p["id"] for p in pulled} and any(p.get("args", {}).get("pin") == "123456" for p in pulled),
          "the bridge (and only the bridge) receives the command with its PIN")
    c.post("/v1/commands/%s/result" % rid, headers=dev_tok[ids[0]], json={"status": "done", "output": "rebooting"})
    st = {h["id"]: h["status"] for h in c.get("/admin/devices/%s/commands" % ids[0], headers=A).json()}
    check(st.get(rid) == "done", "the result comes back to the fleet")

    # ---- set-pin endpoint, alert fixes, rollouts ----------------------------------------------------
    r = c.post("/admin/devices/%s/pin" % ids[1], headers=A, json={"pin": "123456789"})
    check(r.status_code == 400, "a 9-digit PIN is refused by the fleet (the bridge takes 4-8)", r.text)
    r = c.post("/admin/devices/%s/pin" % ids[1], headers=A, json={})
    check(r.status_code == 200 and len(r.json().get("pin", "")) == 6, "…a generated PIN is 6 digits, shown once", r.text)
    tel(ids[2], pin={"pin_set": True, "lockout": True, "lockout_remaining": 1800, "required": True, "protocol": 2},
        quarantined=["bridge-web.py"])
    fixes = {a["kind"]: (a.get("fix") or {}).get("command")
             for a in {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}[ids[2]]["alerts"]}
    check(fixes.get("pin_lockout") == "clear-lockout" and fixes.get("update_rolled_back") == "unquarantine",
          "PIN-lockout and rolled-back-update alerts carry their fix buttons", fixes)
    # An admin is already installing on two bridges ("Install on…") when the rollout starts
    # (2026-09-28): the same version on NB-001, another version on NB-002.
    pre_same = c.post("/admin/devices/%s/commands" % ids[0], headers=A,
                      json={"type": "update", "args": {"version": "2.1.1-abc1234"}, "confirm": True}).json().get("id")
    pre_other = c.post("/admin/devices/%s/commands" % ids[1], headers=A,
                       json={"type": "update", "args": {"version": "2.1.0-fff0000"}, "confirm": True}).json().get("id")
    r = c.post("/admin/rollouts", headers=A, json={"version": "2.1.1-abc1234", "source": BASE + "/payloads/ota/2.1.1-abc1234",
                                                   "stage_pct": 100})
    check(r.status_code == 200, "a rollout can be started", r.text)
    ro_id = r.json().get("id")
    from app.models import Command as _Cmd0
    db.expire_all()
    per_dev = {i: [u.id for u in db.query(_Cmd0).filter(_Cmd0.type == "update", _Cmd0.device_id == i).all()] for i in ids[:3]}
    rov = {x["device_id"]: x for x in c.get("/admin/rollouts/%s" % ro_id, headers=A).json().get("devices", [])}
    check(per_dev[ids[0]] == [pre_same] and rov.get(ids[0], {}).get("command_id") == pre_same,
          "a bridge already installing the same version is not sent a second update: the rollout adopts it", (per_dev, rov.get(ids[0])))
    check(per_dev[ids[1]] == [pre_other] and rov.get(ids[1], {}).get("status") == "queued",
          "a bridge installing another version is left to finish first (stays queued, no second update)", (per_dev, rov.get(ids[1])))
    check(len(per_dev[ids[2]]) == 1 and rov.get(ids[2], {}).get("status") == "dispatched", "an idle bridge gets its update", per_dev)
    check(r.status_code == 200 and "BRIDGE-0004" in r.json().get("excluded", []),
          "the rollout leaves out the unclaimed bridge and names it (it cannot be updated remotely)", r.json().get("excluded"))
    from app.models import Command as _Cmd, RolloutTarget as _RT
    db.expire_all()
    ups = db.query(_Cmd).filter(_Cmd.type == "update").all()
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("bridge_agent", ROOT / "pi/scripts/bridge-agent.py")
    _agent = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_agent)
    check(ups and all(u.timeout_s == M.TIMEOUT_S["update"] for u in ups) and M.TIMEOUT_S["update"] > 120,
          "rollout OS updates get the OS-update timeout, not 120 s (they used to expire mid-download)",
          [u.timeout_s for u in ups])
    check(M.TIMEOUT_S["update"] >= _agent.DETACHED["update"] + 300,
          "the fleet waits longer for an update than the bridge lets it run, plus time to report (%d vs %d)"
          % (M.TIMEOUT_S["update"], _agent.DETACHED["update"]))
    if ups:
        u = ups[0]; u.status = "expired"; db.commit()
        c.get("/admin/rollouts/%s" % ro_id, headers=A)
        db.expire_all()
        t = db.query(_RT).filter(_RT.command_id == u.id).first()
        check(t is not None and t.status == "failed", "an update that never reported back counts as failed (halts widening)",
              t.status if t else None)

    # ---- audit fixes (2026-09-25) ---------------------------------------------------------------
    r = c.post("/admin/devices/%s/commands" % ids[0], headers=A, json={"type": "set-peer", "args": {"ip": "203.0.113.9"}})
    check(r.status_code == 400, "the fleet can no longer point a room's audio anywhere (set-peer refused)", r.text)
    # a bridge on older software: lock would stop all media there, clear-lockout does not exist there
    tel(ids[3], pin={"pin_set": True, "locked": False, "lockout": False, "lockout_remaining": 0})   # no protocol = old image
    r = c.post("/admin/devices/%s/commands" % ids[3], headers=A, json={"type": "lock", "confirm": True})
    check(r.status_code == 409 and "STOPS ALL MEDIA" in r.text, "lock is refused on older bridge software (it stops all media there)", r.text)
    r = c.post("/admin/devices/%s/commands" % ids[3], headers=A, json={"type": "clear-lockout"})
    check(r.status_code == 409, "clear-lockout is refused on older software (it has none)", r.text)
    r = c.post("/admin/commands/broadcast", headers=A, json={"type": "lock", "confirm": True})
    sk = [x["device"] for x in r.json().get("skipped", [])] if r.status_code == 200 else []
    check(r.status_code == 200 and len(sk) == 1 and len(r.json()["queued"]) == 3,
          "a broadcast lock skips the older bridge and says so", r.text)
    r = c.post("/admin/devices/%s/pin" % ids[3], headers=A, json={"pin": "4321"})
    check(r.status_code == 200 and "writes the PIN into its own log" in r.json().get("warning", ""),
          "setting a PIN on older software comes with a warning", r.text)
    # PINs never outlive delivery: expired and cancelled set-pin rows are scrubbed too
    from app.models import Command as _C
    r1 = c.post("/admin/devices/%s/commands" % ids[1], headers=A, json={"type": "set-pin", "args": {"pin": "555555"}}).json()
    c.delete("/admin/devices/%s/commands/%s" % (ids[1], r1["id"]), headers=A)
    r2 = c.post("/admin/devices/%s/commands" % ids[2], headers=A, json={"type": "set-pin", "args": {"pin": "666666"}}).json()
    c.get("/v1/commands", headers=dev_tok[ids[2]])                         # delivered ...
    db.expire_all(); row = db.get(_C, r2["id"]); row.sent_at = row.sent_at - dt.timedelta(seconds=600); db.commit()
    c.get("/admin/devices/%s/commands" % ids[2], headers=A)               # ... never answered: the sweep expires it
    db.expire_all()
    a1, a2 = db.get(_C, r1["id"]), db.get(_C, r2["id"])
    check(a1.status == "cancelled" and "555555" not in json.dumps(a1.args), "a cancelled set-pin keeps no PIN in the database", a1.args)
    check(a2.status == "expired" and "666666" not in json.dumps(a2.args), "an expired set-pin keeps no PIN in the database", (a2.status, a2.args))
    # payloads are downloads, never pages
    r = c.post("/admin/payloads", headers=A, files={"script": ("x", b"#!<script>alert(1)</script>"), "sig": ("s", b"0" * 72)},
               data={"name": "evil.html"})
    check(r.status_code == 400, "an .html payload is refused (it could run as a page on the fleet's origin)", r.text)
    r = c.post("/admin/payloads", headers=A, files={"script": ("x", b"#!/bin/bash\necho hi\n"), "sig": ("s", b"0" * 72)},
               data={"name": "bridge-web-probe.sh"})
    check(r.status_code == 200, "a catalog-shaped payload is accepted", r.text)
    r = c.post("/admin/payloads", headers=A, files={"script": ("x", b"[Service]\nNice=5\n"), "sig": ("s", b"0" * 72)},
               data={"name": "dropin.jitter-sentry"})
    check(r.status_code == 200, "every catalog drop-in name is accepted (dropin.jitter-sentry)", r.text)
    r = c.get("/payloads/bridge-web-probe.sh")
    check(r.status_code == 200 and r.headers.get("content-disposition") == "attachment"
          and r.headers.get("x-content-type-options") == "nosniff" and "sandbox" in r.headers.get("content-security-policy", "")
          and r.headers.get("content-type") == "application/octet-stream",
          "payloads are served as downloads (attachment, nosniff, sandboxed)", dict(r.headers))
    # one bridge's malformed telemetry never breaks the list or the stream
    tel(ids[3], pin="x", quarantined=[1], mesh_path="relay", temp=71.5, services=[["a"], "b"], power="weird")
    r = c.get("/admin/devices", headers=A)
    check(r.status_code == 200 and len(r.json()) == 4, "malformed telemetry from one bridge: the fleet list still answers", r.status_code)
    tel(ids[3])

    # ---- a slow mail server must not lock the database for everyone else ----------------------------
    from app import alerting as AL, notifier as NT
    from app.config import settings as ST
    from app.models import AlertEvent as _AE
    tel(ids[1], temp="91.0'C")                                  # a fresh alert for the loop to email
    orig = (NT.any_channel_configured, NT.deliver, getattr(ST, "alert_notify_after_s", 0))
    NT.any_channel_configured = lambda: True
    ST.alert_notify_after_s = 0                                 # email it on this pass (no hold)
    NT.deliver = lambda payload: (time.sleep(6), {"email": True})[1]     # 6 s > SQLite's 5 s lock wait
    done = {}
    def run_eval():
        s2 = SessionLocal()
        try:
            done["stats"] = AL.evaluate(s2)
        finally:
            s2.close()
    th2 = threading.Thread(target=run_eval, daemon=True); th2.start()
    time.sleep(1.0)                                             # the loop is now inside the "email"
    t0 = time.monotonic(); r = tel(ids[0]); took = time.monotonic() - t0
    th2.join(30)
    NT.any_channel_configured, NT.deliver, ST.alert_notify_after_s = orig
    check(r.status_code == 200 and took < 3, "a bridge's heartbeat is saved at once while an alert email is being sent (%.1f s)" % took,
          (r.status_code, r.text[:120]))
    db.expire_all()
    hot = db.query(_AE).filter(_AE.device_id == ids[1], _AE.kind == "temp_high").first()
    check(done.get("stats", {}).get("notified", 0) >= 1 and hot is not None and hot.notified_at is not None,
          "…and the alert was still emailed and recorded", (done, hot and hot.notified_at))
    tel(ids[1], streams={"video": True, "voice": True, "return": True}, udc="configured")   # live again

    # ---- nb: the terminal tool speaks fleet numbers ---------------------------------------------
    (T / "tok").write_text("ADMIN")
    NBENV = dict(os.environ, FLEET_URL=BASE, FLEET_TOKEN_FILE=str(T / "tok"))
    def nb(*args):
        r = subprocess.run([sys.executable, str(ROOT / "tools/nb")] + list(args), capture_output=True, text=True,
                           env=NBENV, timeout=60)
        return r.returncode, r.stdout + r.stderr
    rc, out = nb("list")
    check(rc == 0 and "NB-001" in out and "NB-007" in out and "LIVE" in out, "nb list shows numbers and states", out[-400:])
    for q in ("NB-007", "nb7", "7", "007"):
        rc, out = nb("show", q)
        check(rc == 0 and "NB-007 Main Hall" in out, "nb show %s finds NB-007" % q, out[:200])
    rc, out = nb("rename", "7", "Main Hall East", "--yes")
    check(rc == 0 and "NB-007 Main Hall East" in out, "nb rename by number", out)
    rc, out = nb("renumber", "7", "1", "--yes")
    check(rc != 0 and "already" in out, "nb renumber refuses a number in use", out)
    rc, out = nb("renumber", "main hall east", "8", "--yes")
    check(rc == 0 and "NB-008" in out, "nb renumber by name", out)

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

    # ---- one crafted Range header must not stall the server (CVE-2025-62727, 2026-09-28) ------------
    # starlette 0.41.3 parsed this header in quadratic time on the only event loop, so every bridge
    # heartbeat, sign-in and panel request waited behind a single anonymous request: the panel at
    # "/" needs no login. Measured here: /healthz answers while the crafted request is in flight.
    # 40 KB took ~5 s on 0.41.3; the cost grows with the square of the length.
    import re, starlette
    req = (BACKEND / "requirements.txt").read_text()
    pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)(?:\[[a-z,]+\])?==([0-9.]+)\s*$", req, re.M))
    ver = lambda v: tuple(int(x) for x in v.split("."))
    check(ver(pins.get("starlette", "0")) >= (1, 3, 1),
          "starlette is pinned in its own right, at 1.3.1 or later (the last advisory's fix)", pins.get("starlette"))
    check(ver(pins.get("python-multipart", "0")) >= (0, 0, 31),
          "python-multipart is pinned at 0.0.31 or later (the last advisory's fix)", pins.get("python-multipart"))
    check(starlette.__version__ == pins.get("starlette"),
          "the server under test runs the pinned starlette (if not, rebuild the test venv from requirements.txt)",
          (starlette.__version__, pins.get("starlette")))
    evil = "bytes=" + "1" * 40000 + "x"
    res = {}
    def attack():
        t0 = time.monotonic()
        try:
            r = httpx.get(BASE + "/", headers={"Range": evil}, timeout=120)
            res["code"], res["body"] = r.status_code, r.text[:80]
        except httpx.HTTPError as e:
            res["code"] = repr(e)
        res["took"] = time.monotonic() - t0
    th3 = threading.Thread(target=attack, daemon=True); th3.start()
    time.sleep(0.3)
    t0 = time.monotonic(); hz = httpx.get(BASE + "/healthz", timeout=120); hz_took = time.monotonic() - t0
    th3.join(120)
    # The header must have reached the file server and been refused THERE (400, naming the Range
    # header): a refusal by the HTTP parser in front of it would make the timing prove nothing.
    check(res.get("code") == 400 and "range" in res.get("body", "").lower(),
          "the crafted header reaches the app's file server, which refuses it", res)
    check(hz.status_code == 200 and hz_took < 1.5 and res.get("took", 99) < 2.5,
          "a 40 KB crafted Range header does not stall the server (healthz %.2f s, the request %.2f s, starlette %s)"
          % (hz_took, res.get("took", -1), starlette.__version__), res)
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
