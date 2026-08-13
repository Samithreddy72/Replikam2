#!/usr/bin/env python3
"""Read a file (or list a directory) on the bridge, remotely. READ ONLY.

WHY THIS EXISTS
---------------
The fleet can run 25 named actions and nothing else, which is the right default: a stolen
admin token cannot run code on the hardware. But it also means that when something
unexpected happens, the honest answer is "I cannot look at that file", and the only way
forward is a 25-minute image rebuild.

That is not hypothetical. On 2026-08-13, answering "what is req_number actually set to?"
required either a full image cycle or a signed script override — and the override route took
the bridge down twice, once by looping and once by wedging systemd, both needing a physical
power cycle. Every question that mattered that night was a READ.

So this adds reading, and only reading. No writes, no shell, no systemctl. If something needs
changing it goes through a named action or a signed OTA, which are the paths that already
have rollback.

WHAT IT REFUSES, AND WHY
------------------------
  outside the allow-listed roots   a remote read of arbitrary paths is a filesystem
                                   exfiltration primitive; the roots are the places that
                                   answer operational questions and nothing else
  symlinks that escape             the path is REALPATH'd before the root check, so a link
                                   inside an allowed root pointing at /etc/shadow is refused
                                   on the resolved path, not the requested one
  device nodes                     /dev/* is not diagnostics; reading one can block forever
                                   or change hardware state
  known-secret files               the agent token, private keys, the Wi-Fi setup password
  anything token-shaped            lines matching a secret pattern are redacted even inside
                                   an allowed file, because a config file can grow a secret
                                   long after this list was written

Size-capped, so a remote operator cannot pull the journal over a venue uplink by accident.

    bridge-read.py <path> [--lines N] [--tail]
    bridge-read.py --list <dir>
"""
import argparse, os, re, stat, sys

# Places that answer operational questions. Deliberately not "/" with exclusions: an
# allow-list fails closed when something new appears, a deny-list fails open.
_ROOTS = ("/proc", "/sys", "/etc/bridge", "/etc/default", "/etc/netbridge",
          "/data", "/var/log", "/usr/local/bin", "/home/pi", "/run")
# Resolve the roots too. Comparing a resolved path against an UNRESOLVED root refuses
# legitimate files whenever a root is itself a symlink — which is already true here, since
# /home/pi/flight.txt is redirected onto /data. Caught by the test, not by reading the code.
ROOTS = tuple(os.path.realpath(r) for r in _ROOTS)

# Files whose whole purpose is to hold a secret. Matched on the RESOLVED path.
# Matched on the RESOLVED path, and resolved themselves. An earlier version compared a
# resolved path against these literal strings, so on any system where a parent is a symlink
# (/etc -> /private/etc) the resolved path no longer matched and the file was ALLOWED. The
# defence must not depend on how a path happens to be spelled.
DENY = tuple(os.path.realpath(p) for p in (
    "/etc/bridge/agent.token",
    "/etc/bridge/setup-wifi-pass",
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers",
))
# By NAME as well as by path, because a credential file is a credential wherever it is
# reached from — and this catches the same file under /data or a future location.
DENY_PAT = (
    re.compile(r"/agent\.token$"),                 # the device's fleet credential
    re.compile(r"/setup-wifi-pass$"),              # the printed setup password
    re.compile(r"/ssh_host_[a-z0-9]+_key$"),       # private host keys (the .pub is fine)
    re.compile(r"\.pem$"),                         # keys and certs
    re.compile(r"/(id_rsa|id_ed25519)$"),
    re.compile(r"/(shadow|gshadow|sudoers)$"),
)

# Redaction net. A file inside an allowed root can acquire a secret long after this list was
# written, so the VALUE of anything token-shaped is stripped even from a permitted read.
SECRET_LINE = re.compile(
    r"(?i)^(\s*[A-Za-z0-9_.\-]*"
    r"(?:token|secret|password|passwd|key|auth|bearer|tskey|credential)"
    r"[A-Za-z0-9_.\-]*\s*[:=]\s*)(\S+)")

MAX_BYTES = int(os.environ.get("READ_MAX_BYTES", "65536"))


def refuse(msg, code=1):
    print("bridge-read: REFUSED — %s" % msg, file=sys.stderr)
    sys.exit(code)


def resolve(path):
    """Resolve first, check second. Checking the requested path would let a symlink inside an
    allowed root point anywhere and pass."""
    if not path.startswith("/"):
        refuse("path must be absolute")
    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in ROOTS):
        refuse("%s resolves to %s, which is outside the readable roots %s"
               % (path, real, ", ".join(ROOTS)))
    if real in DENY or any(p.search(real) for p in DENY_PAT):
        refuse("%s holds a credential" % real)
    return real


def redact(text):
    """Keep the key name, drop the value. Knowing that BOOTSTRAP_TOKEN is set is useful;
    knowing what it is, is the thing worth protecting."""
    out, n = [], 0
    for ln in text.splitlines(True):
        red = SECRET_LINE.sub(lambda m: m.group(1) + "<redacted>", ln)
        if red != ln:
            n += 1
        out.append(red)
    return "".join(out), n


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("path")
    ap.add_argument("--list", action="store_true", help="list a directory instead of reading")
    ap.add_argument("--lines", type=int, default=0, help="only the first N lines")
    ap.add_argument("--tail", action="store_true", help="take --lines from the END")
    a = ap.parse_args()

    real = resolve(a.path)

    if a.list:
        if not os.path.isdir(real):
            refuse("%s is not a directory" % real)
        print("bridge-read: listing %s" % real)
        for name in sorted(os.listdir(real))[:500]:
            p = os.path.join(real, name)
            try:
                st = os.lstat(p)
                kind = ("dir " if stat.S_ISDIR(st.st_mode) else
                        "link" if stat.S_ISLNK(st.st_mode) else
                        "file")
                print("  %-4s %10d  %s" % (kind, st.st_size, name))
            except Exception:
                print("  ?              %s" % name)
        return 0

    try:
        st = os.stat(real)
    except Exception as e:
        refuse("cannot stat %s (%s)" % (real, e))
    # /proc and /sys entries are not regular files, so allow those; block devices and FIFOs,
    # which can block forever or have side effects when read.
    if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) or stat.S_ISFIFO(st.st_mode):
        refuse("%s is a device or pipe, not a readable file" % real)
    if stat.S_ISDIR(st.st_mode):
        refuse("%s is a directory — use --list" % real)

    try:
        with open(real, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except Exception as e:
        refuse("cannot read %s (%s)" % (real, e))

    truncated = len(data) > MAX_BYTES
    text = data[:MAX_BYTES].decode("utf-8", "replace")

    if a.lines > 0:
        lines = text.splitlines(True)
        text = "".join(lines[-a.lines:] if a.tail else lines[:a.lines])

    text, redacted = redact(text)

    print("bridge-read: %s  (%d bytes%s)"
          % (real, st.st_size, ", truncated to %d" % MAX_BYTES if truncated else ""))
    if redacted:
        print("bridge-read: %d line(s) redacted — a value looked like a credential" % redacted)
    print("--- begin ---")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        print()
    print("--- end ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
