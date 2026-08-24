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
DISK = (ROOT / "factory" / "build-disk-image.sh").read_text()

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

print("\nA flashed card knows its own name and version")
print("=============================================")

print("\n  ---- the early exit no longer eats the identity work ----")
exit_at = FB.find("no provision conf found")
host_at = FB.find("hostnamectl set-hostname")
ver_at  = FB.find(">/etc/bridge/version")
if exit_at == -1:
    no("the provision-conf guard vanished entirely")
else:
    if host_at != -1 and host_at < exit_at:
        ok("hostname is set BEFORE the early exit")
    else:
        no("hostname rename still sits after the exit — every card stays 'raspberrypi'")
    if ver_at != -1 and ver_at < exit_at:
        ok("version is stamped BEFORE the early exit")
    else:
        no("version stamp still sits after the exit — every card reports 'dev'")

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
if 'elif [ ! -s /etc/bridge/version ]; then' in FB:
    ok("firstboot fills in 'dev' only when no seed and no provisioning value exist")
else:
    no("no fallback — a card with neither would report nothing at all")

print("\n  ---- the hostname is derived, not invented ----")
if "pairing-code" in FB and "sha256sum" in FB:
    ok("named from the pairing code, falling back to the CPU serial")
else:
    no("hostname no longer derives from a stable per-device identifier")

print("\n  ---- negative control ----")
# Moving the guard above the identity work must break these tests, or they prove nothing.
if exit_at != -1 and host_at != -1 and ver_at != -1:
    ok("all three markers found, so their ORDER is what is being tested")
else:
    no("a marker is missing; the ordering check would silently pass")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
