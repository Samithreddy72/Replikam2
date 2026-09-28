#!/usr/bin/env python3
"""Staged OS rollouts, run for real (2026-09-28): a bridge counts as updated only when its trial
boot COMMITS the new version.

What the 2026-09-28 audit found, and this proves fixed:
  * "done" from the bridge only means STAGED (bridge-update.sh --fleet exits 0 before the trial
    boot), yet it counted as updated: an image whose trial rolled back on every bridge still read
    "1 of 1 updated · 0 rollbacks" and widened to the whole fleet;
  * a bridge refusing because its meeting laptop was attached counted as a ROLLBACK and halted the
    rollout, was never retried, and paged "OS update failed";
  * at the final wave the panel offered "Finish" only while finishing was refused, and finishing
    with a bridge still queued answered 200 and did nothing - the next rollout was blocked;
  * forgetting a bridge mid-rollout wedged the rollout for ever (and the re-enrolled card kept
    the previous owner's uptime rollups); the live stream never told the page its commands went;
  * resume/pause accepted any state: resuming an aborted rollout made two active at once;
  * the version pickers listed the OLDEST version first and nothing refused a downgrade;
  * nobody could see which bridges a rollout left out, or which one failed and why;
  * a rollout only noticed an expired update when somebody opened the panel.

Starts the actual control plane (uvicorn) on a random local port against a throwaway SQLite
database and talks to it over HTTP exactly as the panel, nb and a bridge do. Needs FastAPI: uses
~/netbridge/fleet-test-venv when the system Python lacks it."""
import os, pathlib, sys

try:
    import fastapi, httpx, uvicorn  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIP  FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import datetime as dt, json, re, socket, sqlite3, subprocess, tempfile, threading, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "control-plane/backend"
T = pathlib.Path(tempfile.mkdtemp())
ENV = dict(os.environ, DATABASE_URL="sqlite:///%s" % (T / "fleet.db"), BOOTSTRAP_TOKENS="boot-test",
           PAYLOAD_DIR=str(T / "payloads"), OFFLINE_AFTER_S="60", ALERT_EVAL_INTERVAL_S="3600")
os.environ.update(ENV)
sys.path.insert(0, str(BACKEND))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:400]) if detail else ""))

print("NetBridge staged rollouts")
print("=========================\n")

# ---- a fleet database written by the build before this one upgrades in place -----------------------
OLD = T / "old.db"
con = sqlite3.connect(OLD)
con.executescript("""
CREATE TABLE rollouts (id INTEGER PRIMARY KEY, org_id VARCHAR, version VARCHAR, source VARCHAR,
                       stage_pct INTEGER, status VARCHAR, created_by VARCHAR, created_at DATETIME, updated_at DATETIME);
CREATE TABLE rollout_targets (rollout_id INTEGER, device_id VARCHAR, status VARCHAR, command_id INTEGER,
                              wave INTEGER, updated_at DATETIME, PRIMARY KEY (rollout_id, device_id));
INSERT INTO rollouts VALUES (1, 'default', '2.2.1-3333333', 'x', 100, 'completed', 'a@b', '2026-09-20', '2026-09-20');
INSERT INTO rollout_targets VALUES (1, 'dev-old', 'succeeded', NULL, 100, '2026-09-20');
""")
con.commit(); con.close()
mig = subprocess.run([sys.executable, "-c", "import app.main"], cwd=str(BACKEND), capture_output=True, text=True,
                     env=dict(ENV, DATABASE_URL="sqlite:///%s" % OLD, PAYLOAD_DIR=str(T / "old-payloads")))
con = sqlite3.connect(OLD)
rcols = {r[1] for r in con.execute("PRAGMA table_info(rollouts)")}
tcols = {r[1] for r in con.execute("PRAGMA table_info(rollout_targets)")}
kept = con.execute("SELECT status FROM rollout_targets WHERE device_id = 'dev-old'").fetchone()
con.close()
check(mig.returncode == 0 and "excluded" in rcols and "reason" in tcols and kept == ("succeeded",),
      "an existing fleet database gains the new rollout columns in place, keeping its rows",
      (mig.returncode, mig.stderr[-300:], sorted(rcols), sorted(tcols), kept))

from app import main as M
from app.models import User, Device, Command, RolloutTarget, TelemetryRollup, utcnow
from app.auth import hash_token
from app.db import SessionLocal
db = SessionLocal()
db.add(User(email="admin@test", role="admin", org_id="default", token_hash=hash_token("ADMIN")))
db.commit()


def publish(v, built="2026-09-28"):
    """The fleet's OS catalog, as tools/publish-ota.sh leaves it."""
    d = T / "payloads/ota" / v
    d.mkdir(parents=True, exist_ok=True)
    # A whole version: the catalog lists only what a bridge can install - an image present, of the
    # size the manifest names, with its hash (publish tools fix, 2026-09-28).
    img = ("netbridge-os %s" % v).encode()
    (d / "rootfs.tar.zst").write_bytes(img)
    (d / "manifest.txt").write_text("version=%s\nimage=rootfs.tar.zst\nsha256=%s\nsize=%d\nbuilt=%s\n"
                                    % (v, __import__("hashlib").sha256(img).hexdigest(), len(img), built))
    (d / "manifest.txt.sig").write_bytes(b"0" * 72)


for v, built in (("2.1.0-9e88a12", "2026-09-17"), ("2.2.0-1111111", "2026-09-24"), ("2.2.1-4444444", "2026-09-27"),
                 ("2.2.1-3333333", "2026-09-28"), ("2.10.0-2222222", "2026-09-28")):
    publish(v, built)

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
c = httpx.Client(base_url=BASE, timeout=15)
(T / "tok").write_text("ADMIN\n")
NBENV = dict(os.environ, FLEET_URL=BASE, FLEET_TOKEN_FILE=str(T / "tok"))
SERIALS = ["20000000bbbb0001", "20000000bbbb0002", "20000000bbbb0003", "20000000bbbb0004", "20000000bbbb0005"]
R1, R2, R3, R4, R5 = SERIALS
tok = {}
STATE = {sid: {"version": "2.2.0-1111111"} for sid in SERIALS}


def enroll(sid, i):
    r = c.post("/v1/enroll", json={"bootstrap_token": "boot-test", "device_id": sid, "pairing_code": "BRIDGE-R%03d" % i,
                                   "version": "2.2.0-1111111", "tailscale_ip": "100.64.1.%d" % i, "hostname": "nb-r%d" % i})
    tok[sid] = {"Authorization": "Bearer " + r.json()["device_token"]}


def tel(sid, **kw):
    """A heartbeat. Keyword changes stick (like a real bridge's state) until changed again. The
    reported version never changes: like a real bridge, it names the image the card was flashed with."""
    STATE[sid].update(kw)
    body = {"ip": "192.168.9.%d" % (SERIALS.index(sid) + 10), "udc": "not attached",
            "streams": {"video": False, "voice": False, "return": False},
            "pin": {"pin_set": True, "locked": True, "lockout": False, "required": True, "protocol": 2}}
    body.update(STATE[sid])
    return c.post("/v1/telemetry", headers=tok[sid], json=body)


def pull(sid):
    return c.get("/v1/commands", headers=tok[sid]).json()


def report(sid, cid, status, output):
    return c.post("/v1/commands/%s/result" % cid, headers=tok[sid], json={"status": status, "output": output})


def view(ro_id):
    return c.get("/admin/rollouts/%s" % ro_id, headers=A).json()


def target(ro_id, sid):
    return next((d for d in view(ro_id).get("devices", []) if d["device_id"] == sid), None)


def ota(state, ver, detail=""):
    return {"state": state, "version": ver, "detail": detail, "ts": time.time()}


def stage(sid, ver):
    """The bridge collects its update, stages the slot and reports what bridge-update.sh --fleet says."""
    got = [x for x in pull(sid) if x["type"] == "update"]
    for x in got:
        tel(sid, ota=ota("staged", ver, "slot B holds %s; trial boot in 45 s" % ver))
        report(sid, x["id"], "done", "[ota] STAGED %s in slot B — trial boot in 45 s; it commits itself if healthy, "
                                     "rolls back if not" % ver)
    time.sleep(0.05)
    tel(sid)                                          # the first heartbeat after the result: still staged
    return got


def commit(sid, ver):
    tel(sid, ota=ota("committed", ver, "slot B is now the permanent slot"))


def finish_all(ro_id, sids, ver):
    for sid in sids:
        stage(sid, ver)
        commit(sid, ver)
    return view(ro_id)


def set_target(ro_id, sid, **kw):
    s2 = SessionLocal()
    t = s2.get(RolloutTarget, {"rollout_id": ro_id, "device_id": sid})
    for k, v in kw.items():
        setattr(t, k, v)
    s2.commit(); s2.close()


def set_cmd(cid, **kw):
    s2 = SessionLocal()
    x = s2.get(Command, cid)
    for k, v in kw.items():
        setattr(x, k, v)
    s2.commit(); s2.close()


def updates(sid, *statuses):
    s2 = SessionLocal()
    q = s2.query(Command).filter(Command.type == "update", Command.device_id == sid)
    if statuses:
        q = q.filter(Command.status.in_(statuses))
    out = [(x.id, x.status, dict(x.args or {})) for x in q.order_by(Command.id).all()]
    s2.close()
    return out


def start(ver, pct, **kw):
    return c.post("/admin/rollouts", headers=A, json=dict({"version": ver, "source": BASE + "/payloads/ota/" + ver,
                                                            "stage_pct": pct}, **kw))


def nb(*args):
    return subprocess.Popen([sys.executable, str(ROOT / "tools/nb")] + list(args) + ["--yes"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=NBENV)


def nb_run(*args, timeout=60):
    """-> (exit code, output); (None, output) when nb is still waiting after `timeout` s."""
    p = nb(*args)
    try:
        out = p.communicate(timeout=timeout)[0]
        return p.returncode, out
    except subprocess.TimeoutExpired:
        p.kill()
        return None, p.communicate()[0] + "\n[test: nb still waiting after %ds - stopped]" % timeout


def V(d, k, default=None):
    """A field of an API answer - absent on builds that lack it (the check then fails, it does not crash)."""
    return d.get(k, default) if isinstance(d, dict) else default


def tstat(ro_id, sid):
    return V(target(ro_id, sid), "status")


class section:
    """One part of the story. If it stops half-way (a field missing, a request refused) that is
    recorded as a FAIL and the next part still runs - so a broken build shows every check it fails."""
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        print("\n  ---- %s ----" % self.name)

    def __exit__(self, et, e, tb):
        if et is None or not issubclass(et, Exception):
            return False
        check(False, "%s: stopped part-way on %s: %s" % (self.name, et.__name__, str(e)[:200]))
        return True


try:
    for i, sid in enumerate(SERIALS, 1):
        enroll(sid, i)
        tel(sid)
    for sid, name in ((R1, "Alpha"), (R2, "Bravo"), (R3, "Charlie"), (R4, "Delta")):
        c.post("/admin/devices/%s/claim" % sid, headers=A, json={"name": name})
    ro1 = ro2 = ro3 = ro4 = ro5 = ro6 = None
    V1, V2, V3, V4, V5, V6, V7, V8 = ("2.2.1-3333333", "2.10.0-2222222", "2.11.0-5555555", "2.12.0-6666666",
                                      "2.13.0-8888888", "2.14.0-aaaaaaa", "2.15.0-bbbbbbb", "2.16.0-ccccccc")

    with section("the catalog, newest first"):
        vers = [v["version"] for v in c.get("/admin/payloads/ota", headers=A).json()]
        check(vers == ["2.10.0-2222222", "2.2.1-3333333", "2.2.1-4444444", "2.2.0-1111111", "2.1.0-9e88a12"],
              "OS versions are listed newest first, by number (2.10 above 2.2; the later build of one version first; "
              "the oldest last)", vers)

    with section("no silent downgrade"):
        r = c.post("/admin/devices/%s/commands" % R1, headers=A, json={"type": "update", "args": {"version": "2.1.0-9e88a12"}, "confirm": True})
        check(r.status_code == 409 and V(V(r.json(), "detail"), "error") == "downgrade" and "OLDER" in r.text,
              "installing 2.1 on a 2.2 bridge is refused as a downgrade", r.text)
        for cid, _, _ in updates(R1, "pending"):
            c.delete("/admin/devices/%s/commands/%s" % (R1, cid), headers=A)
        r = c.post("/admin/devices/%s/commands" % R1, headers=A, json={"type": "update", "args": {"version": "2.1.0-9e88a12"},
                                                                       "confirm": True, "allow_downgrade": True})
        check(r.status_code == 200, "…and queued when the downgrade is asked for explicitly", r.text)
        c.delete("/admin/devices/%s/commands/%s" % (R1, V(r.json(), "id")), headers=A)
        tel(R2, ota=ota("committed", V1))
        r = c.post("/admin/devices/%s/commands" % R2, headers=A, json={"type": "update", "args": {"version": "2.2.0-1111111"}, "confirm": True})
        check(r.status_code == 409 and V1 in r.text,
              "the version a committed OS update installed counts (the reported version is not rewritten by an OTA)", r.text)
        for cid, _, _ in updates(R2, "pending"):
            c.delete("/admin/devices/%s/commands/%s" % (R2, cid), headers=A)
        rc, out = nb_run("update", "Alpha", "2.1.0-9e88a12", timeout=30)
        check(rc not in (0, None) and "NOT queued" in out and "OLDER" in out and not updates(R1, "pending"),
              "nb update to an older version is refused and says why", out[-400:])
        for cid, _, _ in updates(R1, "pending"):
            c.delete("/admin/devices/%s/commands/%s" % (R1, cid), headers=A)
        p = nb("update", "Alpha", "2.1.0-9e88a12", "--allow-downgrade")
        t0 = time.time()
        while time.time() - t0 < 20 and not updates(R1, "pending"):
            time.sleep(0.2)
        queued = updates(R1, "pending")
        p.kill(); p.communicate()
        check(queued and queued[-1][2] == {"version": "2.1.0-9e88a12"}, "…and queued with nb update --allow-downgrade", queued)
        for cid, _, _ in queued:
            c.delete("/admin/devices/%s/commands/%s" % (R1, cid), headers=A)

    with section("rollout 1 (2.2.1, first wave 25%): staged is not updated; a rollback halts widening"):
        tel(R3, udc="configured")                                         # Charlie: the room's laptop is plugged in
        tel(R4, ota=ota("committed", V2))                                 # Delta: newer
        r = start(V1, 25)
        check(r.status_code == 200, "a rollout starts", r.text)
        ro1 = V(r.json(), "id")
        ex = {V(e, "name"): V(e, "reason") for e in V(r.json(), "excluded", [])}
        check(any("Delta" in str(k) and "newer" in str(v) for k, v in ex.items())
              and any("BRIDGE-R005" in str(k) and "claimed" in str(v) for k, v in ex.items()),
              "bridges on a NEWER version and unclaimed ones are left out, each with its reason", r.json().get("excluded"))
        check(V(view(ro1), "excluded") == r.json().get("excluded"),
              "…and the rollout keeps saying so afterwards (not only in the create answer)", view(ro1))
        check(V(view(ro1), "total") == 2, "Alpha and Charlie are the targets (Bravo is on it already)", view(ro1))
        ups = updates(R1, "pending")
        check(len(ups) == 1 and ups[0][2] == {"version": V1},
              "the update names the VERSION (the bridge refuses a manifest for any other, and fetches from its own fleet address)", ups)

        stage(R1, V1)
        v = view(ro1)
        check(V(v, "updated") == 0 and V(v, "in_flight") == 1 and V(v, "verifying") == 1 and tstat(ro1, R1) == "staged",
              "a bridge that only STAGED the image is not counted as updated - it is verifying", v)
        r = c.post("/admin/rollouts/%s/advance" % ro1, headers=A)
        check(r.status_code == 409 and "trial" in r.text, "widening waits for the trial's verdict", r.text)

        tel(R1, ota=ota("rolled back", V1, "the new slot did not become healthy within 300 s; back on the previous slot"))
        v = view(ro1)
        fd = V(v, "failed_devices", [])
        check(V(v, "updated") == 0 and V(v, "rollbacks") == 1 and V(v, "failed") == 0 and tstat(ro1, R1) == "rolled_back"
              and fd and "Alpha" in fd[0]["device"] and "rolled back" in fd[0]["reason"],
              "a trial that rolled back is a rollback, and the rollout says which bridge and why", v)
        check("1 rollback" in V(v, "summary", "") and "failed" not in V(v, "summary", ""),
              "…the summary counts it as a rollback", V(v, "summary"))
        r = c.post("/admin/rollouts/%s/advance" % ro1, headers=A)
        check(r.status_code == 409 and "Alpha" in r.text, "…and widening halts on it (the '0 rollbacks' guard now sees real rollbacks)", r.text)

    with section("a meeting on is not a rollback"):
        c.post("/admin/rollouts/%s/advance?force=true" % ro1, headers=A)            # 25 -> 50 (still Alpha's wave)
        r = c.post("/admin/rollouts/%s/advance?force=true" % ro1, headers=A)        # 50 -> 100: Charlie's turn
        v = r.json()
        check(r.status_code == 200 and V(v, "stage_pct") == 100 and V(v, "dispatched_now") == 0
              and any("Charlie" in w["device"] and "laptop" in w["reason"] for w in V(v, "queued_busy", [])),
              "a bridge with its meeting laptop attached is not sent the update - queued until idle", v)
        check("queued until idle" in V(v, "summary", "") and V(v, "catch_up") is True,
              "…the summary says so, and catch-up is offered", v)
        tel(R3, udc="not attached")
        v = view(ro1)
        check(V(v, "catch_up") is True and any("Charlie" in n for n in V(v, "queued_ready", [])) and not V(v, "queued_busy"),
              "once it is free, the rollout still offers the catch-up and names it ready (the card used to drop the button)", v)
        r = c.post("/admin/rollouts/%s/dispatch" % ro1, headers=A)
        check(V(r.json(), "dispatched_now") == 1, "the catch-up sends it", r.text)
        got = [x for x in pull(R3) if x["type"] == "update"]
        tel(R3, udc="configured", ota=ota("failed", "", "the meeting laptop is attached — unplug it (or --force) 8"))
        for x in got:
            report(R3, x["id"], "failed", "[ota] ERROR: the meeting laptop is attached — unplug it (or --force) 8")
        v = view(ro1)
        t3 = target(ro1, R3) or {}
        check(got and V(v, "rollbacks") == 1 and V(v, "failed") == 0 and t3.get("status") == "queued"
              and "refused" in (t3.get("reason") or ""),
              "the bridge's refusal (laptop plugged back in) sends it back to the queue - it is NOT a rollback", (v, t3))
        kinds = {a["kind"] for a in {d["id"]: d for d in c.get("/admin/devices", headers=A).json()}[R3]["alerts"]}
        check("os_update_failed" not in kinds, "…and does not page 'OS update failed' (nothing was installed)", kinds)
        tel(R3, udc="not attached")
        c.post("/admin/rollouts/%s/dispatch" % ro1, headers=A)
        stage(R3, V1)
        commit(R3, V1)
        v = view(ro1)
        check(V(v, "updated") == 1 and tstat(ro1, R3) == "succeeded",
              "retried once idle, it stages, its trial COMMITS %s, and only now it counts as updated" % V1, v)

    with section("the rollout state machine"):
        r = c.post("/admin/rollouts/%s/abort" % ro1, headers=A)
        check(r.status_code == 200 and V(r.json(), "status") == "aborted", "a halted rollout can be aborted", r.text)
        for act in ("resume", "pause", "abort"):
            r = c.post("/admin/rollouts/%s/%s" % (ro1, act), headers=A)
            check(r.status_code == 409 and V(view(ro1), "status") == "aborted", "%s on an ABORTED rollout is refused (409)" % act, r.text)
            if V(view(ro1), "status") != "aborted":
                c.post("/admin/rollouts/%s/abort" % ro1, headers=A)               # keep the next part's start clean

    with section("rollout 2: every bridge finishes -> Finish is offered, and works"):
        r = start(V2, 100)
        ro2 = V(r.json(), "id")
        check(r.status_code == 200 and V(r.json(), "total") == 3, "rollout 2 targets Alpha, Bravo and Charlie", r.text)
        r = c.post("/admin/rollouts/%s/pause" % ro2, headers=A)
        check(r.status_code == 200 and V(r.json(), "status") == "paused", "an active rollout can be paused", r.text)
        r = c.post("/admin/rollouts/%s/resume" % ro1, headers=A)
        check(r.status_code == 409, "the aborted one still cannot be resumed while another exists", r.text)
        if V(view(ro1), "status") != "aborted":
            c.post("/admin/rollouts/%s/abort" % ro1, headers=A)
        r = c.post("/admin/rollouts/%s/resume" % ro2, headers=A)
        check(r.status_code == 200 and V(r.json(), "status") == "active", "a paused rollout resumes", r.text)
        v = finish_all(ro2, [R1, R2, R3], V2)
        check(V(v, "updated") == 3 and V(v, "in_flight") == 0 and V(v, "queued") == 0 and V(v, "finishable") is True,
              "every bridge committed: nothing outstanding, and the rollout says it can be finished", v)
        r = c.post("/admin/rollouts/%s/advance" % ro2, headers=A)
        check(r.status_code == 200 and V(r.json(), "status") == "completed", "Finish completes it", r.text)
        r = c.post("/admin/rollouts/%s/resume" % ro2, headers=A)
        check(r.status_code == 409 and V(view(ro2), "status") == "completed", "a COMPLETED rollout cannot be resumed (409)", r.text)
        r = c.post("/admin/rollouts/%s/pause" % ro2, headers=A)
        check(r.status_code == 409, "…nor re-opened by pausing it", r.text)
        statuses = [x["status"] for x in c.get("/admin/rollouts", headers=A).json()]
        check(statuses.count("active") == 0, "no rollout is active now - the next one may start", statuses)
        for x in c.get("/admin/rollouts", headers=A).json():
            if x["status"] in ("active", "paused"):
                c.post("/admin/rollouts/%s/abort" % x["id"], headers=A)

    with section("rollout 3: the final wave with an offline bridge still queued"):
        s2 = SessionLocal(); s2.get(Device, R4).last_seen = utcnow() - dt.timedelta(minutes=10); s2.commit(); s2.close()
        r = start(V3, 100)                                  # V3 is not in this fleet's catalog: hosted elsewhere
        ro3 = V(r.json(), "id")
        ups = updates(R1, "pending")
        check(ups and ups[-1][2] == {"source": BASE + "/payloads/ota/" + V3},
              "a version this fleet does not host is fetched from where the rollout says (as a source)", ups)
        v = finish_all(ro3, [R1, R2, R3], V3)
        check(V(v, "updated") == 3 and any("Delta" in n for n in V(v, "queued_offline", [])) and V(v, "finishable") is False,
              "Delta is offline and still queued: not finishable yet", v)
        r = c.post("/admin/rollouts/%s/advance" % ro3, headers=A)
        check(r.status_code == 409 and "Delta" in r.text and V(view(ro3), "status") == "active",
              "finishing with a bridge still queued is refused and says who (it used to answer 200 and do nothing)", r.text)
        r = c.post("/admin/rollouts/%s/advance?force=true" % ro3, headers=A)
        check(r.status_code == 200 and V(r.json(), "status") == "completed", "…and 'Finish anyway' (force) completes it", r.text)
        if V(view(ro3), "status") in ("active", "paused"):
            c.post("/admin/rollouts/%s/abort" % ro3, headers=A)

    with section("rollout 4: trial verdicts"):
        tel(R4)                                                            # Delta back online
        r = start(V4, 100)
        ro4 = V(r.json(), "id")
        check(r.status_code == 200 and V(r.json(), "total") == 4 and V(r.json(), "dispatched_now") == 4,
              "rollout 4 goes to all four", r.text)
        stage(R1, V4)
        commit(R1, "2.12.0-7777777")
        t1 = target(ro4, R1) or {}
        check(t1.get("status") == "failed" and "not %s" % V4 in (t1.get("reason") or ""),
              "a trial that committed ANOTHER version is not counted as this one", t1)
        check("1 failed" in V(view(ro4), "summary", "") and "0 rollbacks" in V(view(ro4), "summary", ""),
              "…and the summary calls it failed, not a rollback", V(view(ro4), "summary"))
        stage(R2, V4)
        tel(R2, udc="configured")                                          # waiting: a meeting started after staging
        check(tstat(ro4, R2) == "staged", "Bravo staged the image and is waiting for its trial")
        set_target(ro4, R2, updated_at=utcnow() - dt.timedelta(minutes=25))
        check(tstat(ro4, R2) == "staged", "a staged bridge in a meeting is NOT failed by the trial window")
        tel(R2, udc="not attached", ota=ota("failed", "", "a presenter session is live — try again after it (or --force) 8"))
        check(tstat(ro4, R2) == "staged",
              "a refusal of some LATER install (no version named) is not taken for this trial's verdict")
        commit(R2, V4)
        check(tstat(ro4, R2) == "succeeded", "…and it counts once its trial commits")
        stage(R3, V4)
        check(tstat(ro4, R3) == "staged", "Charlie staged the image and is waiting for its trial")
        set_target(ro4, R3, updated_at=utcnow() - dt.timedelta(minutes=25))
        t3 = target(ro4, R3) or {}
        check(t3.get("status") == "failed" and "no verdict" in (t3.get("reason") or ""),
              "a bridge online and idle for 20+ min with no verdict (power cut mid-trial) is failed, with the reason", t3)

    with section("forgetting a bridge mid-rollout"):
        s2 = SessionLocal()
        s2.add(TelemetryRollup(device_id=R4, hour=utcnow().replace(minute=0, second=0, microsecond=0), samples=240, up_minutes=60,
                               first_ts=utcnow(), last_ts=utcnow()))
        s2.commit()
        r4_cmds = [x.id for x in s2.query(Command).filter(Command.device_id == R4).all()]
        s2.close()
        check(tstat(ro4, R4) == "dispatched", "Delta's update is in flight when it is removed")
        got, stop = [], threading.Event()
        def reader():
            with httpx.stream("GET", BASE + "/admin/stream", headers=A, timeout=30) as resp:
                ev = None
                for line in resp.iter_lines():
                    if stop.is_set():
                        return
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: ") and ev:
                        got.append((ev, json.loads(line[6:]))); ev = None
        th = threading.Thread(target=reader, daemon=True); th.start()
        t0 = time.time()
        while time.time() - t0 < 10 and not any(g[0] == "ready" for g in got):
            time.sleep(0.1)
        seen_cmds = {g[1]["id"] for g in got if g[0] == "command"}
        r = c.delete("/admin/devices/%s" % R4, headers=A)
        check(r.status_code == 200, "Delta is removed from the fleet", r.text)
        t0 = time.time()
        while time.time() - t0 < 6 and not any(g[0] == "command_removed" for g in got):
            time.sleep(0.1)
        removed = {g[1]["id"] for g in got if g[0] == "command_removed"}
        check(any(g[0] == "device_removed" and g[1]["id"] == R4 for g in got), "the page is told the bridge is gone")
        check(r4_cmds and set(r4_cmds) & seen_cmds and (set(r4_cmds) & seen_cmds) <= removed,
              "…and that its commands are gone too (a re-enrolled card must not show them)", (r4_cmds, sorted(removed)))
        stop.set()
        v = view(ro4)
        check(V(v, "total") == 3 and V(v, "in_flight") == 0 and all(d["device_id"] != R4 for d in V(v, "devices", [])),
              "its place in the rollout went with it: nothing is left 'updating' for ever", v)
        r = c.post("/admin/rollouts/%s/advance?force=true" % ro4, headers=A)
        check(r.status_code == 200 and V(r.json(), "status") == "completed", "…so the rollout can finish", r.text)
        if V(view(ro4), "status") in ("active", "paused"):
            c.post("/admin/rollouts/%s/abort" % ro4, headers=A)
        s2 = SessionLocal()
        left = s2.query(TelemetryRollup).filter(TelemetryRollup.device_id == R4).count()
        s2.close()
        check(left == 0, "its hourly uptime rollups are deleted with the rest of its history", left)
        enroll(R4, 4); tel(R4)
        check(c.get("/admin/devices/%s/uptime" % R4, headers=A).status_code == 200 and
              not any(d["device_id"] == R4 for d in V(view(ro4), "devices", [])),
              "re-enrolled under the same id, it inherits no rollout place")

    with section("rollout 5: no target is ever stuck on a row that cannot finish"):
        publish(V5)
        r = start(V5, 100)
        ro5 = V(r.json(), "id")
        check(r.status_code == 200 and V(r.json(), "total") == 3,
              "rollout 5 targets Alpha, Bravo and Charlie (Delta is unclaimed now)", r.text)
        s2 = SessionLocal()
        t = s2.get(RolloutTarget, {"rollout_id": ro5, "device_id": R1})
        s2.query(Command).filter(Command.id == t.command_id).delete()      # retention got there first
        s2.commit(); s2.close()
        t1 = target(ro5, R1) or {}
        check(t1.get("status") == "failed" and "no longer exists" in (t1.get("reason") or ""),
              "a target whose command row vanished ends 'failed' with the reason, never 'updating' for ever", t1)
        set_cmd(target(ro5, R2)["command_id"], status="error", output="[ota] something odd")
        t2 = target(ro5, R2) or {}
        check(t2.get("status") == "failed" and "not a result" in (t2.get("reason") or ""),
              "an update an older build left with a status word that is not a result ends 'failed', not 'updating'", t2)
        pull(R3)                                                           # delivered; the result is lost
        # past the fleet's own time limit for an OS update (its number, not a copy: it grew from 1 h to
        # 3 h 10 min when slow venue downloads were cut off mid-write, 2026-09-28)
        set_cmd(target(ro5, R3)["command_id"], sent_at=utcnow() - dt.timedelta(seconds=M.TIMEOUT_S["update"] + 100))
        lst = {x["id"]: x for x in c.get("/admin/rollouts", headers=A).json()}
        fd = {f["device"]: f["reason"] for f in V(lst.get(ro5), "failed_devices", [])}
        check(any("Charlie" in k and "never reported back" in v for k, v in fd.items()),
              "the rollout list itself expires an update that never answered - no panel or command list needed", fd)
        c.post("/admin/rollouts/%s/abort" % ro5, headers=A)

    with section("rollout 6: another install already on its way, or already done"):
        publish(V6)
        for sid in (R1, R2, R3):
            tel(sid, ota=ota("committed", V5))
            for cid, _, _ in updates(sid, "pending"):
                c.delete("/admin/devices/%s/commands/%s" % (sid, cid), headers=A)
        man = c.post("/admin/devices/%s/commands" % R1, headers=A, json={"type": "update", "args": {"version": V6}, "confirm": True}).json()
        other = c.post("/admin/devices/%s/commands" % R2, headers=A, json={"type": "update", "args": {"version": V7}, "confirm": True}).json()
        r = start(V6, 10)                                                  # a 10% wave over 3 bridges: Alpha first
        ro6 = V(r.json(), "id")
        t1 = target(ro6, R1) or {}
        check(V(r.json(), "dispatched_now") == 1 and t1.get("status") == "dispatched" and t1.get("command_id") == man.get("id")
              and len(updates(R1, "pending", "sent")) == 1,
              "the same update already sent from the bridge's page is adopted - no second copy queued beside it", (t1, updates(R1)))
        tel(R3, ota=ota("committed", V6))                                  # Charlie got there another way meanwhile
        for _ in range(3):                                                 # 10 -> 25 -> 50 -> 100
            r = c.post("/admin/rollouts/%s/advance?force=true" % ro6, headers=A)
        v = r.json()
        t3 = target(ro6, R3) or {}
        check(t3.get("status") == "succeeded" and "already on" in (t3.get("reason") or "") and not updates(R3, "pending", "sent"),
              "a bridge that meanwhile reached the version counts as updated and is not sent it again", t3)
        w = {x["device"]: x["reason"] for x in V(v, "queued_busy", [])}
        check(tstat(ro6, R2) == "queued" and any("Bravo" in k and "another OS update" in r_ and V7 in r_ for k, r_ in w.items())
              and [u[2] for u in updates(R2, "pending")] == [{"version": V7}],
              "a DIFFERENT update in flight on a bridge makes it wait (two installs would share the spare slot)", (w, updates(R2)))
        c.delete("/admin/devices/%s/commands/%s" % (R2, other.get("id")), headers=A)
        stage(R1, V6)
        commit(R1, V6)
        check(tstat(ro6, R1) == "succeeded", "the adopted update's trial verdict counts for the rollout")
        c.post("/admin/rollouts/%s/abort" % ro6, headers=A)

    with section("nb update waits for the TRIAL's verdict, not the command's 'done'"):
        tel(R1, ota=ota("committed", V6))
        for cid, _, _ in updates(R1, "pending"):
            c.delete("/admin/devices/%s/commands/%s" % (R1, cid), headers=A)
        for want, verdict, rc_ok in ((V8, "committed", True), ("2.17.0-ddddddd", "rolled back", False)):
            publish(want)
            p = nb("update", "Alpha", want)
            t0 = time.time()
            while time.time() - t0 < 20 and not updates(R1, "pending"):
                time.sleep(0.2)
            stage(R1, want)
            time.sleep(5)                                                  # nb has seen "done" (it polls every 4 s)
            tel(R1, ota=ota(verdict, want, "slot B is now the permanent slot" if verdict == "committed"
                            else "the new slot did not become healthy within 300 s"))
            try:
                out = p.communicate(timeout=60)[0]
            except subprocess.TimeoutExpired:
                p.kill(); out = p.communicate()[0] + "\n[test: nb still waiting after 60 s - stopped]"
            if rc_ok:
                check(p.returncode == 0 and "staged" in out and "✔ committed" in out,
                      "nb update reports staged, then COMMITTED once the trial keeps the new OS (exit 0)", out[-400:])
            else:
                check(p.returncode == 1 and "staged" in out and "rolled back" in out and "✔" not in out,
                      "nb update whose trial rolls back ends 'rolled back' with exit 1 - never a success", out[-400:])

    with section("a custom source is still sent as a source"):
        fake = type("R", (), {"version": "2.2.1-3333333", "source": "https://cdn.example.org/netbridge/2.2.1"})()
        rua = getattr(M, "_rollout_update_args", None)
        check(rua is not None and rua(fake) == {"source": fake.source},
              "an image hosted anywhere else is still fetched from where the rollout says")

    with section("the panel shows all of it"):
        PANEL = (ROOT / "control-plane/panel-dist/index.html").read_text()
        lr = re.search(r"async function loadRollouts\(auto\)\{(.*?)\n\}\n", PANEL, re.S)
        body = lr.group(1) if lr else ""
        check("ro.finishable" in body and "Finish anyway" in body and 'mk("Finish", "advance")' in body,
              "the card offers Finish when the rollout is finishable, and 'Finish anyway…' otherwise")
        check("ro.catch_up" in body, "the card offers the catch-up whenever the wave still has bridges to send")
        check("failed_devices" in body and "f.reason" in body and "ro.excluded" in body and "queued_busy" in body,
              "the card names the bridges that did not update (with why), the ones waiting, and the ones left out")
        check(body and "bridge(s)" not in body, "no 'bridge(s)' copy on the card")
        check(re.search(r"setInterval\(\(\) => \{ if \(ADMIN && PAGE === \"updates\"[^\n]*loadRollouts\(true\)", PANEL),
              "the cards refresh themselves while the updates page is open (a trial verdict arrives on its own)")
        sr = re.search(r'\$\("startRolloutBtn"\)\.addEventListener\("click", async \(\) => \{(.*?)\n\}\);', PANEL, re.S)
        sbody = sr.group(1) if sr else ""
        check("i === 0" in sbody and "r.excluded" in sbody and "allow_downgrade" in sbody and "modern" in sbody
              and "verOlder" in sbody,
              "the start dialog preselects the newest version, previews who is left out (old or newer), names them "
              "after, and can include a downgrade")
        od = re.search(r"async function osUpdateDialog\((.*?)\n\}\n", PANEL, re.S)
        check(od and "meeting laptop is attached" in od.group(1) and "allowDowngrade" in od.group(1) and "verOlder" in od.group(1),
              "the OS update dialog says the laptop refusal is overridden too, warns of a downgrade and offers the box")
        fc = re.search(r"async function followCommand\((.*?)\n\}\n", PANEL, re.S)
        check(fc and "STAGED" in fc.group(1) and 'type === "update"' in fc.group(1) and "followTrial" in fc.group(1),
              "a single OS update that finished is reported as STAGED, never as SUCCEEDED - and its trial is followed")
        ft = re.search(r"async function followTrial\((.*?)\n\}\n", PANEL, re.S)
        check(ft and "last_seen" in ft.group(1) and '"committed"' in ft.group(1) and '"rolled back"' in ft.group(1),
              "…to its verdict, counting only heartbeats after the update finished")
        check(re.search(r'staged \? "staged"', PANEL), "History calls a finished OS update 'staged', not 'succeeded'")
        check('ev === "command_removed"' in PANEL and 'ev === "alert_removed"' in PANEL,
              "the page handles the stream's command_removed / alert_removed")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
