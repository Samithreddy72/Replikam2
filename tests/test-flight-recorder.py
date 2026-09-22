#!/usr/bin/env python3
"""The flight recorder is the power black box: same line format as the shell version, one
line a second, pull= follows the camera pump, the ring trims, and a missing tool never stops
it. Runs the real flight-recorder.py against stub journalctl/vcgencmd.

  python3 tests/test-flight-recorder.py
"""
import importlib.util, os, pathlib, re, stat, subprocess, sys, tempfile, threading, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m):
    global failed; failed += 1; print("  FAIL  %s" % m)

T = pathlib.Path(tempfile.mkdtemp())
(T / "bin").mkdir()
def stub(name, body):
    p = T / "bin" / name; p.write_text("#!/bin/bash\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
# The camera pump reports once a second for ~2 s, then goes quiet.
stub("journalctl", 'for i in 1 2; do echo "pump: ok=20 again=5 err=0(errno=0) gray=0 idle=0 bytesused=460800"; sleep 1; done; sleep 30')
stub("vcgencmd", 'echo "throttled=0x50005"')
os.environ["PATH"] = str(T / "bin") + ":" + os.environ["PATH"]

spec = importlib.util.spec_from_file_location("fr", ROOT / "pi" / "scripts" / "flight-recorder.py")
fr = importlib.util.module_from_spec(spec); spec.loader.exec_module(fr)
real = T / "data-flight.txt"
link = T / "flight.txt"; link.symlink_to(real)      # like /home/pi/flight.txt -> /data
fr.F = str(link); fr.KEEP, fr.CAP = 5, 8
real.write_text("".join("old %d\n" % i for i in range(3)))

threading.Thread(target=fr.main, daemon=True).start()
time.sleep(7.5)
lines = real.read_text().splitlines()
new = [l for l in lines if not l.startswith("old")]

# up= is empty off-Linux (no /proc/uptime); on the bridge it must be a number.
UP = r"\d+" if pathlib.Path("/proc/uptime").exists() else r"\d*"
pat = re.compile(r"^\d\d:\d\d:\d\d up=" + UP + r" udc=\S* ?thr=0x50005 pull=[01]$")
bad = [l for l in new if not pat.match(l)]
(ok if new and not bad else no)("every line keeps the old format (%d lines, bad: %s)" % (len(new), bad[:1]))
(ok if 6 <= len(new) + (len(lines) - len(new)) and len(new) >= 5 else no)(
    "about one line a second (%d new lines in 7.5 s)" % len(new))
pulls = [l.rsplit("=", 1)[1] for l in new]
(ok if pulls[:2] == ["1", "1"] or pulls[1:3] == ["1", "1"] else no)("pull=1 while the pump reports (%s)" % pulls)
(ok if pulls[-1] == "0" else no)("pull drops to 0 once the pump has been quiet > 4 s (%s)" % pulls)
(ok if len(lines) <= fr.CAP else no)("ring trimmed to the cap (%d lines on disk, cap %d)" % (len(lines), fr.CAP))
(ok if link.is_symlink() else no)("rotation keeps the symlink, rewrites the real file")
(ok if not (T / ".flight.rotate.tmp").exists() else no)("no rotation temp file left behind")

# A bridge without vcgencmd must keep recording, with an empty thr= field.
(T / "bin" / "vcgencmd").unlink()
os.environ["PATH"] = str(T / "bin") + ":/usr/bin:/bin"
v = fr.throttled()
(ok if v == "" else no)("missing vcgencmd -> empty thr, no crash (%r)" % v)

# The shell wrapper must hand straight to the Python recorder (name kept for unit + audits).
w = (ROOT / "pi" / "scripts" / "flight-recorder.sh").read_text()
(ok if re.search(r"^exec /usr/bin/python3 /usr/local/bin/flight-recorder.py$", w, re.M) else no)(
    "flight-recorder.sh execs the Python recorder")

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
