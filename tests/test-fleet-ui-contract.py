#!/usr/bin/env python3
"""The fleet UI must be reachable end to end, and must never imply success it cannot prove.

The fleet scored 9/10 on backend evidence: command lifecycle, authorization, idempotency,
confirmation gates, agent parity. None of that answers the question an operator actually has,
which is whether the BUTTON works.

Two gaps were found by asking it:

  1. NO COMMAND OUTCOME. Every action ended at `toast("queued … command #N")` and that was the
     last the operator heard. A command that EXPIRED, FAILED or was refused by the device
     looked identical to one that succeeded. awaitCommand() already existed and was wired to
     exactly one action (jitter-diagnose); reboot and golden-restore reported nothing.

  2. NO WAY TO CANCEL. The DELETE endpoint was built after an unrecallable reboot reached a
     live bridge during an audit — and nothing in the UI could reach it. The toast even showed
     the command id the operator had no means of acting on.

This checks the UI/backend/agent chain as a whole. It parses the panel rather than trusting it,
and it checks the JS with node when available.
"""
import json, pathlib, re, shutil, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _source import code_only

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = (ROOT / "control-plane/panel-dist/index.html").read_text()
BE = (ROOT / "control-plane/backend/app/main.py").read_text()
AG = (ROOT / "pi/scripts/bridge-agent.py").read_text()
P = F = 0


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


def pyset(txt, name):
    m = re.search(name + r"\s*=\s*\{(.*?)\}", txt, re.S)
    return set(re.findall(r'"([a-z0-9-]+)"', m.group(1))) if m else set()


def jsset(txt, name):
    m = re.search(name + r"\s*=\s*new Set\(\[(.*?)\]\)", txt, re.S)
    return set(re.findall(r'"([a-z0-9-]+)"', m.group(1))) if m else set()


print("NetBridge fleet UI contract")
print("===========================\n")

print("  ---- the panel must be valid JavaScript ----")
js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", PANEL, re.S))
node = shutil.which("node")
if node:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
        tmp = fh.name
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    pathlib.Path(tmp).unlink(missing_ok=True)
    if r.returncode == 0:
        ok("node --check passes (a panel that does not parse serves nothing)")
    else:
        no("the panel JavaScript does not parse", (r.stderr or "")[:200])
else:
    print("  (node unavailable — skipping the syntax check, NOT counting it as a pass)")

print("\n  ---- every UI action must exist at the backend and the agent ----")
acts = re.search(r"const ACTIONS = \[(.*?)\n\];", PANEL, re.S)
ui = set()
if acts:
    for cmd, _lbl in re.findall(r'\[\s*"([^"]*)"\s*,\s*"([^"]*)"', acts.group(1)):
        if cmd and not cmd.startswith("#"):
            ui.add(cmd.split(":")[0])
backend = pyset(BE, "ALLOWED_COMMANDS")
m = re.search(r"ALLOWED\s*=\s*\{(.*?)\n\}", AG, re.S)
agent = set(re.findall(r'^\s*"([a-z0-9-]+)":', m.group(1), re.M)) if m else set()

# mesh-key is deliberately NOT a device command: it is a control-plane action with its own
# endpoint. Verified in the panel rather than assumed.
special = {"mesh-key"} if 'type === "mesh-key"' in PANEL else set()
if special:
    ok("mesh-key is special-cased as a control-plane action, not a queued command")

gap = sorted((ui - special) - backend)
if not gap:
    ok("all %d queued UI actions exist in ALLOWED_COMMANDS" % len(ui - special))
else:
    no("UI offers commands the backend will reject: %s" % gap,
       "the button queues and the API answers 400 — a control that cannot work")

gap2 = sorted((ui - special) - agent)
if not gap2:
    ok("all queued UI actions exist in the agent's ALLOWED")
else:
    no("UI offers commands the DEVICE will refuse: %s" % gap2,
       "the command queues successfully and then does nothing")

print("\n  ---- confirmation must be enforced at both ends ----")
uiconf = jsset(PANEL, "BACKEND_CONFIRM")
beconf = pyset(BE, "CONFIRM_REQUIRED")
if uiconf == beconf:
    ok("panel and backend confirm-sets are identical (%d commands)" % len(beconf))
else:
    no("confirm sets differ", "only in UI: %s | only in backend: %s"
       % (sorted(uiconf - beconf), sorted(beconf - uiconf)))
if 'confirm: confirmed' in PANEL:
    ok("the panel actually SENDS confirm (a browser dialog is not a control)")
else:
    no("the panel confirms in-browser but never sends it — every destructive button would 400")

print("\n  ---- the operator must learn the OUTCOME, not just that it was queued ----")
if "followCommand" in PANEL:
    ok("issued commands are followed to a terminal state")
else:
    no("commands end at 'queued' and are never reported again",
       "expired, failed and refused all look like success")

body = re.search(r"async function followCommand\((.*?)\n}\n", PANEL, re.S)
if not body:
    no("could not read followCommand")
else:
    b = body.group(1)
    for state in ("SUCCEEDED", "EXPIRED", "cancelled"):
        if state in b:
            ok("reports `%s`" % state)
        else:
            no("no `%s` outcome" % state)
    if "UNKNOWN" in b:
        ok("a command with no result inside the window is reported UNKNOWN, never as success")
    else:
        no("silence must not be reported as success",
           "this is the single rule this project keeps relearning")

print("\n  ---- a queued command must be cancellable from the UI ----")
if 'method:"DELETE"' in PANEL and "/commands/" in PANEL:
    ok("the panel can reach the command-cancel endpoint")
else:
    no("the cancel endpoint is unreachable from the UI",
       "it was built because an unrecallable reboot hit a live bridge; a button never followed")

if re.search(r'status !== "pending"', PANEL):
    ok("cancellation is only offered while the command is still `pending`")
else:
    no("cancel must not be offered once the device already has the command",
       "offering to recall something already delivered is a promise that cannot be kept")

if "409" in PANEL:
    ok("a too-late cancel (409) is reported honestly rather than as success")
else:
    no("409 from the cancel endpoint must not be swallowed")

print("\n  ---- deduplicated commands must be visible as such ----")
if "deduplicated" in PANEL:
    ok("a deduplicated response is reported, not passed off as a fresh action")
else:
    no("a returned existing command id would read as a new action having been taken")

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
