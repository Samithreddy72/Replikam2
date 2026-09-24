#!/usr/bin/env python3
"""Launching the app a second time must never touch the running copy (2026-09-25).

The start-up sweep kills every netbridge-mesh helper and our ffmpeg/GStreamer processes as
"orphans". When a copy was already running those were ITS processes: a double-click on the
launcher cut a live meeting's video, voice and room audio, and only then did the new copy say
"already running" and quit. The check for another copy must come first.

  python3 tests/test-app-single-instance.py
"""
import pathlib, socket, sys
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app" / "netbridge-source"))
import source_app as app

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)) if detail else ""))

print("\nA second launch leaves the running copy alone")
print("=============================================")
running = socket.socket(); running.bind(("127.0.0.1", 0)); running.listen(1)   # "the copy already running"
port = running.getsockname()[1]
killed, opened, applied = [], [], []
with patch.object(app, "PORT", port), \
     patch.object(app, "_kill_orphan_mesh", lambda **k: killed.append("mesh")), \
     patch.object(app, "_kill_orphan_media", lambda: killed.append("media")), \
     patch.object(app, "apply_staged_update", lambda: applied.append(1)), \
     patch.object(app, "_open_browser", lambda url: opened.append(url)), \
     patch.object(app, "load_state", lambda: {}):
    try:
        app.main(); code = "returned"
    except SystemExit as e:
        code = e.code
check(code in (0, None), "the second copy exits cleanly", code)
check(not killed, "…WITHOUT killing any mesh helper or media process of the running copy", killed)
check(not applied, "…and without installing a staged update over the running copy", applied)
check(opened == ["http://127.0.0.1:%d/" % port], "…and opens the running copy's page instead", opened)
running.close()
with patch.object(app, "PORT", port):
    check(app._already_running() is False, "nothing listening -> not running (a normal start proceeds)")

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
