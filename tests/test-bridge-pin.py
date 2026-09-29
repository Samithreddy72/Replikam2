#!/usr/bin/env python3
"""bridge-pin, run for real (2026-09-25): is the PIN SURELY required to go live?

The owner went live without typing a PIN. This drives the actual pi/scripts/bridge-pin against a
throwaway /etc/bridge and /run, with a fake `nft` that keeps the media gate's state the way the
kernel would, and fake `logger`, `bridge`, `systemctl` and `sudo` that record every call - so it
can prove that a PIN never reaches a log and that no media service is ever started, stopped or
restarted by the PIN gate (the old tool restarted all media on every unlock).

  python3 tests/test-bridge-pin.py
"""
import hashlib, json, os, pathlib, stat, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "pi" / "scripts" / "bridge-pin"
T = pathlib.Path(tempfile.mkdtemp())
ETC, RUN, BIN = T / "etc", T / "run", T / "bin"
for d in (ETC, RUN, BIN):
    d.mkdir()
NFTSTATE = T / "nft.json"
CALLS = T / "calls.log"

# ---- fakes --------------------------------------------------------------------------------------
FAKE_NFT = r'''#!/usr/bin/env python3
import json, os, re, sys
S = %(state)r
LOG = %(calls)r
def load():
    try: return json.load(open(S))
    except Exception: return {"exists": False}
def save(st): json.dump(st, open(S, "w"))
args = sys.argv[1:]
st = load()
with open(LOG, "a") as f: f.write("nft " + " ".join(args) + "\n")
if args[:2] == ["-f", "-"]:
    script = sys.stdin.read()
    with open(S + ".scripts", "a") as f: f.write(script + "\n----\n")
    if st.get("broken"):
        print("Error: syntax error", file=sys.stderr); sys.exit(1)
    if "delete table inet netbridge_gate" in script:
        body = script.split("delete table inet netbridge_gate", 1)[1]
        st = {"exists": True, "peer4": [], "peer6": [], "video_in": 0, "voice_in": 0, "refused_in": 0,
              "rules": body}
        for fam in ("peer4", "peer6"):
            m = re.search(r"set %%s \{[^}]*?elements = \{ ([^}]*) \}" %% fam, body)
            if m: st[fam] = [x.strip() for x in m.group(1).split(",")]
        save(st); sys.exit(0)
    if not st.get("exists"):
        print("Error: No such file or directory; table netbridge_gate", file=sys.stderr); sys.exit(1)
    for line in script.splitlines():
        m = re.match(r"flush set inet netbridge_gate (peer[46])$", line.strip())
        if m: st[m.group(1)] = []
        m = re.match(r"add element inet netbridge_gate (peer[46]) \{ (.+) \}$", line.strip())
        if m: st[m.group(1)].append(m.group(2).strip())
    save(st); sys.exit(0)
if args[:1] == ["list"]:
    if not st.get("exists"): sys.exit(1)
    if args[1] == "table":
        out = "table inet netbridge_gate {\n"
        for c in ("video_in", "voice_in", "refused_in"):
            out += "\tcounter %%s {\n\t\tpackets %%d bytes %%d\n\t}\n" %% (c, st[c], st[c] * 900)
        for fam, t in (("peer4", "ipv4_addr"), ("peer6", "ipv6_addr")):
            out += "\tset %%s {\n\t\ttype %%s\n" %% (fam, t)
            if st[fam]: out += "\t\telements = { %%s }\n" %% ", ".join(st[fam])
            out += "\t}\n"
        out += "\tchain media_in {\n\t\ttype filter hook input priority -5; policy accept;\n\t}\n}\n"
        print(out); sys.exit(0)
    if args[1] == "counter":
        print("table inet netbridge_gate {\n\tcounter %%s {\n\t\tpackets %%d bytes %%d\n\t}\n}" %% (
            args[4], st[args[4]], st[args[4]] * 900)); sys.exit(0)
sys.exit(2)
''' % {"state": str(NFTSTATE), "calls": str(CALLS)}

RECORDER = '''#!/bin/sh
echo "%(name)s $*" >> %(calls)s
exit 0
'''
(BIN / "nft").write_text(FAKE_NFT)
for name in ("logger", "bridge", "systemctl", "sudo"):
    (BIN / name).write_text(RECORDER % {"name": name, "calls": CALLS})
for f in BIN.iterdir():
    f.chmod(0o755)

ENV = dict(os.environ, BRIDGE_PIN_ETC=str(ETC), BRIDGE_PIN_RUN=str(RUN),
           BRIDGE_PIN_NFT=str(BIN / "nft"), PATH="%s:%s" % (BIN, os.environ.get("PATH", "")))

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:400]) if detail else ""))

def pin(*args, stdin=None, env=None):
    p = subprocess.run([sys.executable, str(TOOL)] + list(args), input=stdin, capture_output=True,
                       text=True, env=env or ENV, timeout=60)
    try:
        j = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except (ValueError, IndexError):
        j = {}
    return p.returncode, j, p.stdout + p.stderr

def nftstate():
    try:
        return json.loads(NFTSTATE.read_text())
    except (OSError, ValueError):
        return {}

def state():
    return pin("state")[1]

def session_file():
    return json.loads((RUN / "session.json").read_text())

def edit_session(**kw):
    s = session_file(); s.update(kw)
    (RUN / "session.json").write_text(json.dumps(s))

def calls():
    return CALLS.read_text() if CALLS.exists() else ""

A, B = "100.101.1.10", "100.101.1.20"

print("\nbridge-pin - the PIN is required to go live")
print("===========================================")

print("\n  ---- boot: gate closed, no session ----")
rc, j, o = pin("gate-init")
check(rc == 0 and nftstate().get("exists") and nftstate().get("peer4") == [], "gate-init arms the media gate CLOSED", o)
st = state()
check(st.get("protocol") == 2 and st.get("required") is True, "state says: protocol 2, PIN required", st)
check(st.get("locked") is True and st["session"]["active"] is False and st.get("gate") == "closed",
      "no session at boot, gate closed", st)

print("\n  ---- no PIN set = nobody goes live ----")
check(st.get("pin_set") is False, "fresh bridge reports pin_set false")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="1234\n")
check(rc == 5 and j.get("reason") == "no_pin" and "ticket" not in j, "unlock refused: no PIN set (exit 5)", o)
check(nftstate().get("peer4") == [], "…and the gate stayed closed")
check(pin("status")[2].strip() == "no-pin", "status: no-pin")

print("\n  ---- set ----")
for bad in ("12", "123456789", "12ab", ""):
    rc, j, o = pin("set", "-", stdin=bad + "\n")
    check(rc == 6, "set refuses %r (4-8 digits only)" % bad, o)
rc, j, o = pin("set", "-", stdin="246810\n")
check(rc == 0, "set 246810 from stdin", o)
h = (ETC / "pin.hash").read_text().strip()
check(h.startswith("pbkdf2-sha256$200000$") and "246810" not in h, "stored as salted PBKDF2, never the PIN", h[:40])
check(stat.S_IMODE((ETC / "pin.hash").stat().st_mode) == 0o600, "hash file is 0600")
rc, j, o = pin("set", "-", stdin="246810\n")
check((ETC / "pin.hash").read_text().strip() != h, "same PIN twice -> different salt, different hash")
check(state().get("hash") == "pbkdf2" and state().get("pin_set") is True, "state: pin_set, pbkdf2")

print("\n  ---- wrong PINs, lockout, clear-lockout ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="111111\n")
check(rc == 1 and j.get("attempt") == 1 and j.get("attempts_left") == 2 and "ticket" not in j, "wrong PIN: try 1 of 3", o)
rc, j, o = pin("unlock", "-", "--peer", A, stdin="abc\n")
check(rc == 6, "a malformed PIN is refused without costing a try")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="222222\n")
check(rc == 1 and j.get("attempt") == 2, "wrong PIN: try 2 of 3")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="333333\n")
check(rc == 2 and j.get("reason") == "locked_out_now", "third wrong PIN locks the bridge for 1 h", o)
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n")
check(rc == 3 and j.get("reason") == "locked_out" and "ticket" not in j, "even the RIGHT PIN is refused during a lockout", o)
st = state()
check(st.get("lockout") is True and 3500 < st.get("lockout_remaining", 0) <= 3600, "state shows the lockout and time left", st)
check(nftstate().get("peer4") == [], "gate still closed")
rc, j, o = pin("clear-lockout")
check(rc == 0 and not (ETC / "pin.lockout").exists() and not (ETC / "pin.tries").exists(), "admin clear-lockout lifts it", o)

print("\n  ---- the right PIN opens ONE session ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n")
T1 = j.get("ticket", "")
check(rc == 0 and len(T1) == 64 and all(c in "0123456789abcdef" for c in T1), "right PIN -> a 256-bit ticket", o)
check(j.get("peer") == A and j.get("gate") == "open" and nftstate().get("peer4") == [A],
      "the gate admits ONLY this presenter (%s)" % A, nftstate())
sf = (RUN / "session.json")
check(stat.S_IMODE(sf.stat().st_mode) == 0o600 and T1 not in sf.read_text()
      and hashlib.sha256(T1.encode()).hexdigest() in sf.read_text(), "session file is root-only and holds the ticket's HASH, not the ticket")
pub = (RUN / "state.json").read_text()
check(stat.S_IMODE((RUN / "state.json").stat().st_mode) == 0o644 and T1 not in pub and "ticket" not in pub
      and "pbkdf2-sha256$" not in pub, "public state file has no ticket and no hash")
st = state()
check(st["session"]["active"] and st["session"]["peer"] == A and st["locked"] is False, "state: session active for A", st)
check(pin("status")[2].strip() == "live", "status: live")

print("\n  ---- the ticket is what go-live checks ----")
rc, j, o = pin("check", "-", "--peer", A, stdin=T1 + "\n")
check(rc == 0 and j.get("ok"), "check: A's own ticket from A -> ok", o)
rc, j, o = pin("check", "-", "--peer", A, stdin="0" * 64 + "\n")
check(rc == 11 and j.get("reason") == "invalid", "a made-up ticket is refused (exit 11)", o)
rc, j, o = pin("check", "-", "--peer", A, stdin="\n")
check(rc == 11, "no ticket is refused")
rc, j, o = pin("check", "-", "--peer", B, stdin=T1 + "\n")
check(rc == 13 and j.get("reason") == "peer_mismatch", "A's ticket from another address is refused without --rebind", o)
rc, j, o = pin("check", "-", "--peer", B, "--rebind", stdin=T1 + "\n")
check(rc == 0 and j.get("moved") and nftstate().get("peer4") == [B],
      "…with --rebind (the app's mesh helper restarted) the gate follows to the new address", nftstate())
rc, j, o = pin("check", "-", "--peer", A, "--rebind", stdin=T1 + "\n")
check(rc == 0 and nftstate().get("peer4") == [A], "…and back")
rc, j, o = pin("check", "-", "--peer", "192.168.1.50", stdin=T1 + "\n")
check(rc == 4 and j.get("reason") == "bad_peer", "a LAN address can never be a media peer (never LAN)", o)
rc, j, o = pin("unlock", "-", "--peer", "::ffff:100.101.1.30", stdin="246810\n")
check(rc == 0 and j.get("peer") == "100.101.1.30", "IPv4-mapped caller address is normalised", o)
T1b = j.get("ticket", "")

print("\n  ---- last PIN wins; the previous presenter is told why ----")
rc, j, o = pin("unlock", "-", "--peer", B, stdin="246810\n")
T2 = j.get("ticket", "")
check(rc == 0 and j.get("superseded") is True and nftstate().get("peer4") == [B], "B enters the PIN: B has the bridge now", o)
rc, j, o = pin("check", "-", "--peer", "100.101.1.30", stdin=T1b + "\n")
check(rc == 11 and j.get("reason") == "superseded", "the previous presenter's ticket says 'superseded'", o)
check(state().get("last_end", {}).get("reason") == "superseded", "last_end records why")

print("\n  ---- Stop ends the session ----")
rc, j, o = pin("end", "-", stdin=T1 + "\n")
check(rc == 0 and j.get("ended") is False and state()["session"]["active"], "a stale ticket cannot end the current session", o)
rc, j, o = pin("end", "-", stdin=T2 + "\n")
check(rc == 0 and j.get("ended") is True and nftstate().get("peer4") == [], "B's Stop ends it and closes the gate", o)
rc, j, o = pin("check", "-", "--peer", B, stdin=T2 + "\n")
check(rc == 10 and j.get("reason") == "no_session", "after Stop the ticket is dead (exit 10)", o)
check(state()["locked"] is True and state()["last_end"]["reason"] == "stop", "state: locked, last end = stop")

print("\n  ---- 10 min without video relocks; video keeps it open ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n"); T3 = j.get("ticket", "")
edit_session(last_video_m=time.monotonic() - 601)
st = nftstate(); st["video_in"] += 500; NFTSTATE.write_text(json.dumps(st))
pin("sweep")
check(state()["session"]["active"] and state()["session"]["idle_s"] < 5, "video counted since the last look -> still open")
edit_session(last_video_m=time.monotonic() - 601)
pin("sweep")
check(not state()["session"]["active"] and state()["last_end"]["reason"] == "idle"
      and nftstate().get("peer4") == [], "no video for 10 min -> relocked, gate closed")
rc, j, o = pin("check", "-", "--peer", A, stdin=T3 + "\n")
check(rc == 10, "…and that ticket no longer works")

print("\n  ---- 12 h limit ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n"); T4 = j.get("ticket", "")
edit_session(created_m=time.monotonic() - 12 * 3600 - 5)
rc, j, o = pin("check", "-", "--peer", A, stdin=T4 + "\n")
check(rc == 12 and j.get("why") == "max_age" and nftstate().get("peer4") == [], "after 12 h the ticket expires and the gate closes", o)

print("\n  ---- admin lock ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n"); T5 = j.get("ticket", "")
rc, j, o = pin("lock")
check(rc == 0 and "ended" in o and nftstate().get("peer4") == [], "lock ends the live session and closes the gate", o)
check(pin("check", "-", "--peer", A, stdin=T5 + "\n")[0] == 10, "…the presenter's ticket is dead")

print("\n  ---- changing the PIN ends the live session ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n"); T5b = j.get("ticket", "")
rc, j, o = pin("set", "-", stdin="246810\n")
check(rc == 0 and "live session was ended" in o and nftstate().get("peer4") == [], "a new PIN ends the live session and closes the gate", o)
check(pin("check", "-", "--peer", A, stdin=T5b + "\n")[0] == 10 and state()["last_end"]["reason"] == "pin_changed",
      "…the old ticket is dead (last end: pin_changed)")

print("\n  ---- storage that cannot count a try refuses every PIN ----")
os.chmod(ETC, 0o555)
try:
    wrongs = [pin("unlock", "-", "--peer", A, stdin="999999\n") for _ in range(4)]
    right = pin("unlock", "-", "--peer", A, stdin="246810\n")
finally:
    os.chmod(ETC, 0o755)
check(all(w[0] == 4 and w[1].get("reason") == "error" for w in wrongs), "wrong PINs are refused as errors, not quietly uncounted", [w[1] for w in wrongs][:1])
check(right[0] == 4 and "ticket" not in right[1], "…and even the RIGHT PIN is refused until the count can be kept (fails closed)", right[1])
check(state().get("lockout") is False and nftstate().get("peer4") == [], "no session was opened meanwhile")

print("\n  ---- the gate heals itself ----")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="246810\n"); T6 = j.get("ticket", "")
NFTSTATE.write_text(json.dumps({"exists": False}))          # something flushed the ruleset
pin("sweep")
check(nftstate().get("exists") and nftstate().get("peer4") == [A], "sweep re-arms a vanished gate, still admitting only A", nftstate())
st = nftstate(); st["peer4"] = [A, B]; NFTSTATE.write_text(json.dumps(st))   # tampered set
pin("sweep")
check(nftstate().get("peer4") == [A], "sweep repairs a gate that admits someone else", nftstate())
pin("end", "-", stdin=T6 + "\n")

print("\n  ---- old unsalted hash (the previous tool) is upgraded ----")
(ETC / "pin.hash").write_text(hashlib.sha256(b"4321").hexdigest() + "\n")
(ETC / "pin.locked").write_text("")
check(state().get("hash") == "legacy", "legacy hash detected")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="4321\n"); T7 = j.get("ticket", "")
check(rc == 0 and (ETC / "pin.hash").read_text().startswith("pbkdf2-sha256$"), "right PIN on a legacy hash works and upgrades it", o)
check(not (ETC / "pin.locked").exists(), "the old tool's flag file is removed")
pin("end", "-", stdin=T7 + "\n")
rc, j, o = pin("unlock", "-", "--peer", A, stdin="4321\n")
check(rc == 0, "the same PIN still works after the upgrade"); pin("end", "-", stdin=j.get("ticket", "") + "\n")
rc, j, o = pin("set", "567890")
check(rc == 0, "compat: `bridge-pin set 567890` (argv) still works for an operator at a console")

print("\n  ---- no nftables: PIN still required, alert-worthy, never a false relock ----")
env2 = dict(ENV, BRIDGE_PIN_NFT=str(T / "no-such-nft"))
rc, j, o = pin("gate-init", env=env2)
check(rc == 0 and pin("state", env=env2)[1].get("gate") == "unavailable", "gate-init reports gate=unavailable (never fails the boot unit)", o)
rc, j, o = pin("unlock", "-", "--peer", A, stdin="000000\n", env=env2)
check(rc == 1, "…the PIN is still checked")
pin("clear-lockout", env=env2)
rc, j, o = pin("unlock", "-", "--peer", A, stdin="567890\n", env=env2); T8 = j.get("ticket", "")
check(rc == 0 and j.get("gate") == "unavailable", "…a session still needs the right PIN", o)
edit_session(last_video_m=time.monotonic() - 3600)
pin("sweep", env=env2)
check(pin("state", env=env2)[1]["session"]["active"], "…and an unmeasurable session is NOT relocked for 'idle'")
pin("end", "-", stdin=T8 + "\n", env=env2)
pin("gate-init")

print("\n  ---- nothing leaks, nothing restarts ----")
log = calls()
secrets_seen = [s for s in ("246810", "4321", "567890", "111111", T1, T2, T3, T4, T5, T6, T7) if s and s in log]
check(not secrets_seen, "no PIN and no ticket in any logger line or command line (%d calls recorded)" % log.count("\n"), secrets_seen)
media = [l for l in log.splitlines() if l.split(" ", 1)[0] in ("bridge", "systemctl", "sudo")]
check(not media, "the PIN gate never ran `bridge`, systemctl or sudo - no media start/stop/restart", media[:5])
scripts = (T / "nft.json.scripts").read_text()
check("udp dport 5000" in scripts and "udp dport 5002" in scripts and "5004" not in scripts,
      "the gate filters only the incoming video/voice ports - nothing the bridge sends", "")
check('udp dport { 5000, 5002 } counter name "refused_in" drop' in scripts and 'iifname "lo" accept' in scripts,
      "everyone else is dropped on those two ports; the bridge's own loopback is untouched")

# Real session lifecycle drives the camera's non-secret marker without service restarts.
pin("set", "-", stdin="567890\n")
rc, binding, _ = pin("unlock", "-", "--peer", A, stdin="567890\n")
marker = (RUN / "video-session").read_text().split()
check(rc == 0 and marker[0] == hashlib.sha256(binding["ticket"].encode()).hexdigest()[:16]
      and marker[1] == "1", "unlock publishes this session's video epoch")
pin("end", "-", stdin=binding["ticket"] + "\n")
check((RUN / "video-session").read_text() == "0 0 0\n", "Stop clears video independently of the media processes")
pin("unlock", "-", "--peer", A, stdin="567890\n")
pin("gate-init")
check((RUN / "video-session").read_text() == "0 0 0\n", "gate initialization clears any surviving video marker")

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
