#!/usr/bin/env python3
"""The start-up / go-live sweeps kill only OUR helper and OUR media processes (2026-09-25).

_kill_orphan_mesh used `pgrep -f netbridge-mesh`, which matches whole command lines: at every
launch and every go-live the app SIGKILLed any process that merely MENTIONED the helper - a
Terminal grep, an editor, a build, a Claude session's shell (that is how it was found).
_kill_orphan_media had the same weakness for its three argument signatures. Now the mesh sweep
matches the process NAME exactly, and the media sweep kills a signature match only in a process
that really is ffmpeg / gst-launch-1.0.

Part 1 checks the logic with the process table faked. Part 2 runs REAL processes: a compiled
sleeper under a unique name plays the helper (or the media process), and a shell whose arguments
merely mention that name plays the bystander. Unique names only: this test can never touch a
real NetBridge helper, even with the app live.

  python3 tests/test-app-orphan-sweep.py
"""
import os, pathlib, random, shutil, signal, subprocess, sys, tempfile, time
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


class Out:
    def __init__(self, stdout=""):
        self.stdout, self.returncode = stdout, 0 if stdout else 1


print("\nPart 1 - the logic, with the process table faked")
print("================================================")
calls = []
with patch.object(app, "IS_WIN", False), \
     patch.object(app.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Out())[1]):
    app._kill_orphan_mesh()
check(["pgrep", "-x", "netbridge-mesh"] in calls, "the mesh sweep asks for the process NAME exactly (pgrep -x netbridge-mesh)", calls)
check(not any("-f" in c for c in calls), "…and never matches whole command lines (no pgrep -f)", calls)

sent = []
with patch.object(app, "IS_WIN", False), \
     patch.object(app.subprocess, "run", lambda cmd, **kw: Out("201\n202\n")), \
     patch.object(app.os, "kill", lambda pid, sig: sent.append((pid, sig))):
    app._kill_orphan_mesh(exclude_pid=201)
check(sent == [(202, signal.SIGTERM), (202, signal.SIGKILL)],
      "the helper the app keeps (exclude_pid) is spared; an orphan gets TERM then KILL, as before", sent)

names = {101: "zsh", 102: "ffmpeg", 103: "gst-launch-1.0", 104: "grep", 105: ""}
sent = []
with patch.object(app, "IS_WIN", False), \
     patch.object(app.subprocess, "run", lambda cmd, **kw: Out("101\n102\n103\n104\n105\n")), \
     patch.object(app, "_proc_name", lambda pid: names.get(pid, "")), \
     patch.object(app.os, "kill", lambda pid, sig: sent.append((pid, sig))):
    app._kill_orphan_media()
check(sorted({p for p, _ in sent}) == [102, 103],
      "the media sweep kills a signature match only in ffmpeg / gst-launch-1.0 (not zsh, grep, or a vanished pid)", sent)
check(all(s == signal.SIGKILL for _, s in sent), "…with the same signal as before (SIGKILL)", sent)

calls = []
with patch.object(app, "IS_WIN", True), patch.object(app, "MESH_PROC", "netbridge-mesh.exe"), \
     patch.object(app.subprocess, "run", lambda cmd, **kw: calls.append(cmd)):
    app._kill_orphan_mesh()
    app._kill_orphan_media()
check(calls == [["taskkill", "/F", "/IM", "netbridge-mesh.exe"]],
      "Windows: the mesh sweep is taskkill by image name and the media sweep does nothing (both unchanged)", calls)

print("\nPart 2 - real processes")
print("=======================")
cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
if app.IS_WIN or not cc:
    print("  (not run here: needs macOS/Linux and a C compiler)")
else:
    tmp = tempfile.mkdtemp()
    tag = "%04d" % random.randint(0, 9999)
    mesh_name, media_name, mark = "nbtestmesh" + tag, "nbtestmedia" + tag, "nbtest-mark-" + tag
    src = os.path.join(tmp, "sleeper.c")
    with open(src, "w") as f:
        f.write("#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n")
    for n in (mesh_name, media_name):
        subprocess.run([cc, "-o", os.path.join(tmp, n), src], check=True, capture_output=True)
    procs = []

    def spawn(argv):
        p = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        return p

    def gone(p, s=2.0):
        t = time.monotonic() + s
        while time.monotonic() < t:
            if p.poll() is not None:
                return True
            time.sleep(0.05)
        return False

    try:
        helper = spawn([os.path.join(tmp, mesh_name)])
        bystander = spawn(["/bin/sh", "-c", "sleep 60; : " + mesh_name])
        media = spawn([os.path.join(tmp, media_name), "-bsf:v", mark])
        media_bystander = spawn(["/bin/sh", "-c", "sleep 60; : " + mark])
        time.sleep(0.5)
        old = subprocess.run(["pgrep", "-f", mesh_name], capture_output=True, text=True).stdout.split()
        check(str(helper.pid) in old and str(bystander.pid) in old,
              "the bystander is real: the OLD match (pgrep -f) finds it as well as the helper", old)
        check(app._proc_name(helper.pid) == mesh_name, "_proc_name reads the executable's own name",
              app._proc_name(helper.pid))
        app._kill_orphan_mesh(name=mesh_name)
        check(gone(helper), "the mesh sweep killed the orphaned helper")
        check(bystander.poll() is None, "…and left alone a shell whose arguments merely mention it")
        oldm = subprocess.run(["pgrep", "-f", mark], capture_output=True, text=True).stdout.split()
        check(str(media.pid) in oldm and str(media_bystander.pid) in oldm,
              "the media bystander is real: the OLD match finds it as well", oldm)
        app._kill_orphan_media(names=(media_name,), marks=(mark,))
        check(gone(media), "the media sweep killed the orphaned media process")
        check(media_bystander.poll() is None, "…and left alone a shell carrying the same signature in its arguments")
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
