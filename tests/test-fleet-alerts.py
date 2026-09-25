#!/usr/bin/env python3
"""Alerts bridge by bridge, one page at a time (2026-09-25).

The Alerts page was one flat list - what is firing now plus the newest 50 episodes for the
whole fleet - so one flapping bridge pushed every other bridge's history off the end. Now:
  GET /admin/alerts/bridges   one row per bridge, worst first, with its open alerts and counts
  GET /admin/alerts/episodes  the full history, paginated, by bridge / open-resolved / severity / kind
Starts the real control plane (uvicorn) on a throwaway SQLite database, seeds ~160 episodes on
three bridges plus another organisation's bridge, and checks every number against the seed.
Needs FastAPI (uses ~/netbridge/fleet-test-venv, or the Python you run it with)."""
import os, pathlib, sys

try:
    import fastapi, httpx, uvicorn  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIP  FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import datetime as dt, socket, subprocess, tempfile, time

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

from app import main as M                        # creates tables, migrates
from app.models import User, Device, AlertEvent
from app.auth import hash_token
from app.db import SessionLocal
from app import notifier

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
db = SessionLocal()
db.add_all([User(email="admin@test", role="admin", org_id="default", token_hash=hash_token("ADMIN")),
            User(email="presenter@test", role="presenter", org_id="default", token_hash=hash_token("PRES")),
            User(email="other@test", role="admin", org_id="other", token_hash=hash_token("OTHER"))])
db.add_all([
    Device(id="dev-a", org_id="default", pairing_code="BRIDGE-AAAA", number=1, name="Hall", claimed_at=NOW,
           last_seen=NOW, latest={"pin": {"protocol": 2, "required": True, "pin_set": False}}),
    Device(id="dev-b", org_id="default", pairing_code="BRIDGE-BBBB", number=2, name="Studio", claimed_at=NOW,
           last_seen=NOW, latest={"usb_misses_per_s": 40, "pin": {"protocol": 2, "required": True, "pin_set": True}}),
    Device(id="dev-c", org_id="default", pairing_code="BRIDGE-CCCC"),                   # new, never seen: offline
    Device(id="dev-x", org_id="other", pairing_code="BRIDGE-XXXX", number=1, name="Elsewhere", claimed_at=NOW),
])
db.commit()

SEED = []          # (device, kind, opened, resolved)
KINDS_A = ["offline", "usb_misses", "new_device", "throttled"]
for i in range(137):                              # bridge A: 137 resolved episodes, every 1.7 h back
    opened = NOW - dt.timedelta(hours=1.7 * i + 0.5)
    SEED.append(("dev-a", KINDS_A[i % 4], opened, opened + dt.timedelta(seconds=60 * (i + 1))))
for i in range(20):                               # bridge B: 20 resolved, every 5 h back
    opened = NOW - dt.timedelta(hours=5 * i + 1)
    SEED.append(("dev-b", "usb_misses" if i % 2 else "throttled", opened, opened + dt.timedelta(minutes=5)))
TIE = NOW - dt.timedelta(hours=30)
for _ in range(3):                                # three B episodes opened at the same instant (id breaks the tie)
    SEED.append(("dev-b", "disk_low", TIE, TIE + dt.timedelta(minutes=1)))
SEED.append(("dev-a", "pin_not_set", NOW - dt.timedelta(hours=3), None))   # still open (A is firing pin_not_set)
SEED.append(("dev-c", "offline", NOW - dt.timedelta(hours=5), None))       # still open
SEED.append(("dev-b", "usb_misses", NOW - dt.timedelta(hours=2), None))    # still open (B is firing usb_misses)
# Every alert that is firing has its open episode seeded, so the server's own alert loop (it runs once
# at start-up) finds nothing new to open or resolve and the counts below stay exact.
for i in range(5):                                # the other organisation - must never appear
    SEED.append(("dev-x", "offline", NOW - dt.timedelta(hours=i), None if i == 0 else NOW - dt.timedelta(hours=i) + dt.timedelta(minutes=2)))
for dev, kind, opened, resolved in SEED:
    db.add(AlertEvent(device_id=dev, kind=kind, detail="%s on %s" % (kind, dev), opened_at=opened, resolved_at=resolved,
                      notified_at=opened if kind != "new_device" else None))
db.commit()
MINE = [s for s in SEED if s[0] != "dev-x"]

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

def get(path, who="ADMIN", **params):
    return httpx.get(BASE + path, params=params, headers={"Authorization": "Bearer " + who} if who else {}, timeout=20)
def ep(**params):
    r = get("/admin/alerts/episodes", **params)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return r.json()
def ts(iso):
    return dt.datetime.fromisoformat(iso)

try:
    print("\nWho may see it")
    print("==============")
    for path in ("/admin/alerts/bridges", "/admin/alerts/episodes"):
        check(get(path, who=None).status_code == 401, "%s: no token -> 401" % path)
        check(get(path, who="PRES").status_code == 401, "%s: a presenter is refused (401, like every /admin route)" % path)
    check(get("/admin/alerts/episodes", device_id="dev-x").status_code == 404,
          "another organisation's bridge -> 404 (looks exactly like no bridge)")
    check(get("/admin/alerts/episodes", device_id="nope").status_code == 404, "an unknown bridge -> 404")
    x = get("/admin/alerts/episodes", who="OTHER").json()
    check(x["total"] == 5 and {i["device_id"] for i in x["items"]} == {"dev-x"},
          "the other organisation sees only its own 5 episodes", x["total"])

    print("\nPages")
    print("=====")
    p1 = ep()
    check(p1["total"] == len(MINE) and p1["page"] == 1 and p1["page_size"] == 25 and len(p1["items"]) == 25,
          "default: page 1 of 25, total = every episode of my bridges (%d)" % len(MINE), {k: p1[k] for k in ("total", "page", "page_size")})
    check(p1["pages"] == -(-len(MINE) // 25) and p1["has_next"] and not p1["has_prev"] and (p1["first"], p1["last"]) == (1, 25),
          "pages = ceil(total / 25), has_next, not has_prev, showing 1-25", {k: p1[k] for k in ("pages", "first", "last")})
    seen, order_ok, pg = [], True, 1
    while True:
        r = ep(page=pg)
        keys = [(ts(i["opened_at"]), i["id"]) for i in r["items"]]
        order_ok &= keys == sorted(keys, reverse=True)
        seen += [i["id"] for i in r["items"]]
        if not r["has_next"]:
            break
        pg += 1
    check(len(seen) == len(set(seen)) == len(MINE), "walking every page: each episode exactly once, none missing",
          (len(seen), len(set(seen)), len(MINE)))
    check(order_ok, "newest first on every page, with the id breaking ties (stable across pages)")
    ties = [i for i in ep(device_id="dev-b", page_size=100)["items"] if i["kind"] == "disk_low"]
    check(len(ties) == 3 and [i["id"] for i in ties] == sorted((i["id"] for i in ties), reverse=True),
          "three episodes opened at the same instant come out in a fixed order")
    last = ep(page=999)
    check(last["page"] == last["pages"] and len(last["items"]) == len(MINE) - (last["pages"] - 1) * 25 and not last["has_next"],
          "a page past the end returns the last page (not an empty table)", {k: last[k] for k in ("page", "pages")})
    big = ep(page_size=100)
    check(len(big["items"]) == 100 and big["pages"] == -(-len(MINE) // 100), "page_size 100 is allowed")
    for bad, why in (({"page_size": 101}, "page_size above 100"), ({"page_size": 0}, "page_size 0"), ({"page": 0}, "page 0"),
                     ({"status": "maybe"}, "an unknown status"), ({"severity": "loud"}, "an unknown severity"),
                     ({"kind": "x'; drop table alert_events;--"}, "a kind that is not a kind name")):
        check(get("/admin/alerts/episodes", **bad).status_code == 422, "refused (422): %s" % why)

    print("\nFilters")
    print("=======")
    a_all = [s for s in MINE if s[0] == "dev-a"]
    ra = ep(device_id="dev-a", page_size=100)
    check(ra["total"] == len(a_all) and {i["device_id"] for i in ra["items"]} == {"dev-a"},
          "one bridge: only its episodes (%d)" % len(a_all), ra["total"])
    ro = ep(status="open", page_size=100)
    check(ro["total"] == 3 and {(i["device_id"], i["kind"]) for i in ro["items"]} == {("dev-a", "pin_not_set"), ("dev-c", "offline"), ("dev-b", "usb_misses")},
          "open: exactly the three unresolved episodes", [(i["device_id"], i["kind"]) for i in ro["items"]])
    rr = ep(status="resolved")
    check(rr["total"] == len(MINE) - 3 and rr["counts"] == {"open": 3, "resolved": len(MINE) - 3, "all": len(MINE)},
          "resolved: the rest, and the counts for the segmented control add up", rr["counts"])
    crit = ep(severity="critical", page_size=100)
    check(crit["total"] == sum(1 for s in MINE if s[1] in notifier.CRITICAL_KINDS) and all(i["severity"] == "critical" and i["kind"] in notifier.CRITICAL_KINDS for i in crit["items"]),
          "critical: only critical kinds (the same list the email uses)", crit["total"])
    warn = ep(severity="warning", page_size=100)
    check(warn["total"] == sum(1 for s in MINE if s[1] not in notifier.CRITICAL_KINDS and s[1] != "new_device")
          and all(i["severity"] == "warning" for i in warn["items"]), "warning: neither critical nor info", warn["total"])
    info = ep(severity="info", page_size=100)
    check(info["total"] == sum(1 for s in MINE if s[1] == "new_device") and all(i["kind"] == "new_device" for i in info["items"]),
          "info: the 'new bridge enrolled' records only", info["total"])
    kd = ep(kind="throttled", device_id="dev-b", page_size=100)
    check(kd["total"] == sum(1 for s in MINE if s[0] == "dev-b" and s[1] == "throttled"), "kind + bridge together", kd["total"])

    print("\nWhat each row says")
    print("==================")
    newest_a = ep(device_id="dev-a", status="resolved")["items"][0]
    check(newest_a["duration_s"] == 60 and newest_a["opened_at"].endswith("+00:00") and newest_a["resolved_at"].endswith("+00:00"),
          "a resolved episode: exact duration, times in UTC with an offset", newest_a)
    op = [i for i in ro["items"] if i["device_id"] == "dev-a"][0]
    check(abs(op["duration_s"] - 3 * 3600) < 120 and op["resolved_at"] is None, "an open episode: duration so far, no resolved time", op)
    check(op["device"] == "NB-001 · Hall" and [i for i in ro["items"] if i["device_id"] == "dev-c"][0]["device"] == "BRIDGE-CCCC",
          "the bridge is named the way the panel names it (NB-001 · Hall; an unclaimed one by its code)")
    check(newest_a["notified"] is True, "emailed or not is carried through")

    print("\nBridge by bridge")
    print("================")
    b = get("/admin/alerts/bridges").json()
    rows = {r["device_id"]: r for r in b["bridges"]}
    check(set(rows) == {"dev-a", "dev-b", "dev-c"}, "one row per bridge of MY organisation", list(rows))
    check([r["device_id"] for r in b["bridges"]] == ["dev-a", "dev-c", "dev-b"],
          "worst first: critical (by fleet number, unnumbered last), then warnings", [r["device_id"] for r in b["bridges"]])
    for dev in ("dev-a", "dev-b", "dev-c"):
        d = db.get(Device, dev); db.refresh(d)
        live = [a["kind"] for a in M._safe_alerts(d)]
        check([a["kind"] for a in rows[dev]["open"]] == live, "%s: 'open' is exactly what is firing now (%s)" % (dev, live or "nothing"))
    a_pin = [a for a in rows["dev-a"]["open"] if a["kind"] == "pin_not_set"][0]
    check(a_pin["severity"] == "critical" and a_pin["fix"] and a_pin["fix"]["command"] == "set-pin"
          and abs((NOW - ts(a_pin["since"])).total_seconds() - 3 * 3600) < 60,
          "an open alert carries severity, its one-click fix, and since when (from its episode)", a_pin)
    for dev in ("dev-a", "dev-b"):
        want24 = sum(1 for s in MINE if s[0] == dev and s[1] != "new_device" and s[2] >= NOW - dt.timedelta(hours=24))
        want7 = sum(1 for s in MINE if s[0] == dev and s[1] != "new_device" and s[2] >= NOW - dt.timedelta(days=7))
        check((rows[dev]["episodes_24h"], rows[dev]["episodes_7d"]) == (want24, want7),
              "%s: %d episodes in 24 h, %d in 7 days (info records not counted)" % (dev, want24, want7),
              (rows[dev]["episodes_24h"], rows[dev]["episodes_7d"]))
    want_last = max(s[2] for s in MINE if s[0] == "dev-a" and s[1] != "new_device")
    check(abs((ts(rows["dev-a"]["last_alert_at"]) - want_last).total_seconds()) < 1, "last alert time per bridge")
    t = b["totals"]
    check(t == {"bridges": 3, "needing_attention": sum(1 for r in rows.values() if r["open"]),
                "open_critical": sum(r["open_critical"] for r in rows.values()), "open_warning": sum(r["open_warning"] for r in rows.values()),
                "episodes_24h": sum(r["episodes_24h"] for r in rows.values())}, "totals add up", t)

    print("\nEvery alert carries its own fix")
    print("===============================")
    import re
    from app import alerts as AL
    src = (BACKEND / "app/alerts.py").read_text()
    raised = set(re.findall(r'"kind":\s*"([a-z_]+)"', src)) | {"telemetry_unreadable", "new_device"}
    agent_src = (ROOT / "pi/scripts/bridge-agent.py").read_text()
    agent_cmds = set(re.findall(r'^\s+"([a-z][a-z-]*)":\s+lambda', agent_src, re.M))
    check(len(raised) >= 17 and len(agent_cmds) >= 10, "found %d alert kinds in alerts.py and %d agent commands" % (len(raised), len(agent_cmds)))
    missing, bad_cmd, no_steps, no_title = [], [], [], []
    for k in sorted(raised):
        f = AL.alert_fix(k)
        if not f or not (f.get("label") or "").strip():
            missing.append(k); continue
        if not [s for s in f.get("steps") or [] if isinstance(s, str) and len(s.strip()) > 10]:
            no_steps.append(k)
        if f.get("command") and (f["command"] not in M.ALLOWED_COMMANDS or f["command"] not in agent_cmds):
            bad_cmd.append((k, f["command"]))
        if notifier.alert_title(k) == k.replace("_", " ").capitalize() and k not in notifier.TITLES:
            no_title.append(k)
    check(not missing, "every alert kind has a fix with a label", missing)
    check(not no_steps, "every fix says what to do, step by step", no_steps)
    check(not bad_cmd, "every one-click fix names a command the server AND the bridge accept", bad_cmd)
    check(not no_title, "every alert kind has a readable title", no_title)
    nb_src = (ROOT / "tools/nb").read_text()
    nb_fix = set(re.findall(r'"([a-z-]+)": "nb ', nb_src))
    no_nb = sorted({AL.alert_fix(k)["command"] for k in raised if AL.alert_fix(k).get("command")} - nb_fix)
    check(not no_nb, "every one-click fix has its nb command too (nb alerts prints it)", no_nb)
    hands_on = sorted(k for k in raised if not AL.alert_fix(k).get("command"))
    check("offline" in hands_on and "throttled" in hands_on and "temp_high" in hands_on,
          "offline / power / heat get hands-on steps, not a button that cannot work (%d hands-on kinds)" % len(hands_on))
    check(AL.alert_fix("pin_not_set")["command"] == "set-pin" and AL.alert_fix("pin_lockout")["command"] == "clear-lockout",
          "the PIN alerts keep their one-click fixes")
    f1 = AL.alert_fix("offline"); f1["steps"].append("mutated")
    check("mutated" not in AL.alert_fix("offline")["steps"], "a fix handed out is a copy (one caller cannot change it for the next)")
    for dev, row in rows.items():
        for a in row["open"]:
            check(a.get("fix") and a["fix"].get("steps") and a.get("title"), "%s: open alert %s reaches the panel with its fix and title" % (dev, a["kind"]))
    off = [a for a in rows["dev-c"]["open"] if a["kind"] == "offline"][0]
    check(any("BridgeSetup-CCCC" in s for s in off["fix"]["steps"]),
          "the offline fix names THIS bridge's setup Wi-Fi (BridgeSetup-CCCC)", off["fix"]["steps"])
    check(all(i.get("title") for i in ep(page_size=100)["items"]), "every history row carries its title")
    sent = []
    notifier._smtp_send = lambda msg: sent.append(msg)
    from app.config import settings as S_
    S_.smtp_host, S_.alert_email_to, S_.alert_email_from = "smtp.test", "admin@test", "fleet@test"
    D_ = db.get(Device, "dev-a")
    notifier._send_email(notifier.build_message(AL.bridge_title(D_), D_.id, "pin_not_set", "no PIN set", "firing", AL.alert_fix("pin_not_set", D_)))
    notifier._send_email(notifier.build_message(AL.bridge_title(D_), D_.id, "offline", "no heartbeat", "resolved", None))
    body = sent[0].get_content() if sent else ""
    check(len(sent) == 2 and sent[0]["Subject"] == "[NetBridge CRITICAL] NB-001 · Hall — No PIN set"
          and sent[1]["Subject"] == "[NetBridge CRITICAL] NB-001 · Hall — Offline (resolved)",
          "email subjects: severity, NB-001 · Hall, the readable title, (resolved)", [m["Subject"] for m in sent])
    check("Fix    : Set a PIN…  (one click in the fleet panel)" in body and "1. Every bridge needs a PIN" in body,
          "the email carries the fix and its numbered steps", body[-300:])

    print("\nNothing that already used alerts broke")
    print("========================================")
    h = get("/admin/alerts/history")
    check(h.status_code == 200 and len(h.json()) == 50, "/admin/alerts/history still answers (newest 50)")
    check(get("/admin/alerts").status_code == 200, "/admin/alerts still answers")

    print("\nStill quick with a long history")
    print("===============================")
    db.add_all([AlertEvent(device_id="dev-b", kind="usb_misses", opened_at=NOW - dt.timedelta(minutes=7 * i),
                           resolved_at=NOW - dt.timedelta(minutes=7 * i - 3)) for i in range(1, 5001)])
    db.commit()
    t0 = time.monotonic(); r = ep(page=150); t1 = time.monotonic(); get("/admin/alerts/bridges"); t2 = time.monotonic()
    check(r["total"] == len(MINE) + 5000 and t1 - t0 < 1.5 and t2 - t1 < 1.5,
          "5,000 more episodes: a deep page in %.2f s, the bridge summary in %.2f s" % (t1 - t0, t2 - t1))
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
