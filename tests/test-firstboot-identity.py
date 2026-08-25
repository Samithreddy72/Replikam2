#!/usr/bin/env python3
"""Does a freshly flashed card end up with a name and a version?

WHY THIS EXISTS
---------------
Neither happened, on any card, for weeks.

bridge-firstboot.sh reads an optional provisioning file and bails out early when there is
none:

    [ -f "$CONF" ] || { log "no provision conf found; nothing to do"; exit 0; }

These images do not ship that file - the fleet config is seeded onto /data at build time
instead - so the early exit fires on EVERY card. The hostname rename and the version stamp
sat below it and never ran. Every bridge stayed "raspberrypi" (colliding on mDNS the moment
two share a venue LAN) and every bridge reported version "dev".

Enrolment worked the whole time, which is why nobody noticed. On 2026-08-24 identifying which
image a bridge was running took fingerprinting an unrelated bug in its status output, and
read-file then confirmed the file simply was not there:

    bridge-read: REFUSED - cannot stat /etc/bridge/version (No such file or directory)

The version also has to be written to the right PLACE: fstab binds /data/etc-bridge over
/etc/bridge, so what the image build writes into the rootfs is shadowed at runtime.

  python3 tests/test-firstboot-identity.py
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FB = (ROOT / "pi" / "scripts" / "bridge-firstboot.sh").read_text()
# The identity work moved out of firstboot on 2026-08-25. It had worked only by accident:
# firstboot's `systemctl disable` is unreachable while no provisioning file exists, so the
# script happened to run every boot. On a read-only root nothing holds the hostname, so it
# must be set at each boot by something that ALWAYS runs - not by a script that would
# self-disable the day a provision conf appeared.
ID = (ROOT / "pi" / "scripts" / "bridge-identity.sh").read_text()
UNIT = (ROOT / "pi" / "systemd" / "bridge-identity.service").read_text()
DISK = (ROOT / "factory" / "build-disk-image.sh").read_text()

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

print("\nA flashed card knows its own name and version")
print("=============================================")

print("\n  ---- identity does not depend on firstboot's fate ----")
exit_at = FB.find("no provision conf found")
call_at = FB.find("bridge-identity.sh")
if exit_at > 0 and 0 < call_at < exit_at:
    ok("firstboot invokes the identity step BEFORE its early exit")
else:
    no("identity is behind the exit that fires on every card", (call_at, exit_at))
if "WantedBy=multi-user.target" in UNIT and "Type=oneshot" in UNIT:
    ok("identity also has its own unit, so it runs even if firstboot self-disables")
else:
    no("identity would stop being applied the day firstboot disables itself")
if "RemainAfterExit=no" in UNIT:
    ok("it runs on EVERY boot — the only persistence a read-only root allows")
else:
    no("would run once and never again; the kernel hostname does not survive a reboot")
if "hostnamectl set-hostname" in ID and ">/etc/bridge/version" in ID:
    ok("both hostname and version live in the always-run script")
else:
    no("the identity script is missing one of them")

print("\n  ---- the version is written where it can actually be read ----")
# fstab: /data/etc-bridge -> /etc/bridge. Writing the rootfs copy is writing to a path that
# disappears the moment /data mounts.
if re.search(r"/data/etc-bridge\s+/etc/bridge\s+none\s+bind", DISK):
    ok("confirmed: /etc/bridge is a bind mount from /data/etc-bridge")
else:
    no("the bind is gone — this test's whole premise needs rechecking")
if re.search(r'>\s*"\$MNT/p4/etc-bridge/version"', DISK):
    ok("the disk build seeds the BIND SOURCE (/data/etc-bridge/version)")
else:
    no("nothing seeds /data/etc-bridge/version — the runtime copy will not exist")
if 'if [ -n "${VERSION:-}" ]; then' in DISK:
    ok("only writes it when a version was actually supplied")
else:
    no("would write an empty or literal version string")

print("\n  ---- the fallback still exists, but only as a fallback ----")
if 'elif [ ! -s /etc/bridge/version ]; then' in ID:
    ok("firstboot fills in 'dev' only when no seed and no provisioning value exist")
else:
    no("no fallback — a card with neither would report nothing at all")

print("\n  ---- the hostname is derived, not invented ----")
if "pairing-code" in ID and "sha256sum" in ID:
    ok("named from the pairing code, falling back to the CPU serial")
else:
    no("hostname no longer derives from a stable per-device identifier")

print("\n  ---- it does not trust a command that lies ----")
# hostnamectl tries to write /etc/hostname, and on this read-only root it returned 0 while
# changing nothing - so the `||` fallback that WOULD have worked never ran. Every bridge
# shipped as "raspberrypi" as a result.
if 'hostnamectl set-hostname "$NEWHOST" >/dev/null 2>&1 || true' in ID:
    ok("hostnamectl's exit code is explicitly discarded")
else:
    no("still branches on an exit code that reports success and does nothing")
if '[ "$(hostname)" = "$NEWHOST" ] || hostname "$NEWHOST"' in ID:
    ok("falls back to sethostname(2), which works on a read-only root")
else:
    no("no filesystem-free way to set the name")
if 'ERROR: hostname is still' in ID:
    ok("a failure is reported loudly instead of leaving a silent no-op")
else:
    no("would fail silently again")
if "[ -w /etc/hostname ]" in ID and "[ -w /etc/hosts ]" in ID:
    ok("writes to the read-only root are attempted only if writable, never assumed")
else:
    no("would fail or error on a read-only root")

print("\n  ---- negative control ----")
# Moving the guard above the identity work must break these tests, or they prove nothing.
if exit_at != -1 and call_at != -1:
    ok("both markers found, so their ORDER is what is being tested")
else:
    no("a marker is missing; the ordering check would silently pass")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
