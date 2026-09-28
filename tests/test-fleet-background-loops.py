#!/usr/bin/env python3
"""The fleet's background loops start with the app and stop with it (2026-09-28).

Alerts (email/webhook), retention and command/rollout housekeeping all run as tasks the app starts
itself. If they did not start, nothing would fail: the API answers, the panel loads, and simply no
alert goes out, no table is pruned and no rollout moves. They used to hang off `@app.on_event`,
which the FastAPI now pinned deprecates; this test is what notices if a future upgrade drops it.
Runs the app in-process on a throwaway SQLite database. Needs FastAPI (~/netbridge/fleet-test-venv)."""
import os, pathlib, sys, tempfile

try:
    import fastapi, httpx  # noqa: F401
except ImportError:
    venv = pathlib.Path.home() / "netbridge/fleet-test-venv/bin/python"
    if venv.exists() and pathlib.Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), __file__])
    print("  SKIPPED  FastAPI not installed (make ~/netbridge/fleet-test-venv to run this)")
    sys.exit(0)

import asyncio, warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
T = pathlib.Path(tempfile.mkdtemp())
os.environ.update(DATABASE_URL="sqlite:///%s" % (T / "fleet.db"), PAYLOAD_DIR=str(T / "payloads"),
                  ALERT_EVAL_INTERVAL_S="3600", SMTP_HOST="", ALERT_WEBHOOK_URL="")
sys.path.insert(0, str(ROOT / "control-plane/backend"))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:400]) if detail else ""))

LOOPS = {"sweep_loop", "evaluate_loop", "_housekeeping_loop"}
started, cancelled = [], []
real_create_task = asyncio.create_task
def spy(coro, *a, **k):
    task = real_create_task(coro, *a, **k)
    name = getattr(coro, "__qualname__", "?")
    started.append(name)
    task.add_done_callback(lambda t: cancelled.append(name) if t.cancelled() else None)
    return task
asyncio.create_task = spy

print("\nThe fleet's background loops")
print("============================")
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    import app.main as M
check(not [w for w in caught if "on_event" in str(w.message)],
      "the app no longer uses the deprecated @app.on_event", [str(w.message)[:120] for w in caught])

from fastapi.testclient import TestClient
with TestClient(M.app) as client:
    ok = client.get("/healthz").status_code == 200
    running = set(started)
check(ok and LOOPS <= running, "starting the app starts the alert, retention and housekeeping loops", sorted(running))
check(LOOPS <= set(cancelled), "stopping the app stops them (no loop outlives it)", sorted(cancelled))

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
