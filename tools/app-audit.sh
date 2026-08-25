#!/usr/bin/env bash
# Audit the built presenter app BEFORE handing it to anyone.
#
# WHY THIS EXISTS
# ---------------
# The image has been audited before flashing since 2026-08-14; the app never has, and it is
# half the product. Every fault of the last week that was NOT in the bridge was in this
# binary or in how it shuts down: a camera left wedged by a SIGKILL, a supervisor that told
# the operator to go and check the far end of a link that was fine.
#
# It also has to work for someone who has installed nothing. No Python, no ffmpeg, no
# Homebrew — download, double-click, get past Gatekeeper once, go live. So this checks both:
# that the fixes are really in the binary, and that a person with an empty machine can run it.
#
#   bash tools/app-audit.sh                    # audits app/netbridge-source/dist
#   bash tools/app-audit.sh ~/Desktop/NetBridge  # audits an installed copy
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/app/netbridge-source/source_app.py"
DIR="${1:-$HERE/app/netbridge-source/dist}"
APP="$DIR/NetBridgeSource"; MESH="$DIR/netbridge-mesh"

PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
no()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
warn() { WARN=$((WARN+1)); printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
sec()  { printf '\n\033[1m── %s\033[0m\n' "$1"; }
srcgrep() { grep -qE "$1" "$SRC" 2>/dev/null; }
# Print one function's body. An awk/sed line range cannot do this reliably: the opening
# "def foo" line also matches a closing "^    def" pattern, so the range ends where it began
# and every check inside it silently fails. That produced two false FAILs on 2026-08-24.
body() { python3 - "$SRC" "$1" <<'PYB'
import ast, sys
src = open(sys.argv[1]).read()
want = sys.argv[2]
for n in ast.walk(ast.parse(src)):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == want:
        print(ast.get_source_segment(src, n) or "")
        break
PYB
}

VER=$(grep -oE 'APP_VERSION = "[0-9.]+"' "$SRC" | grep -oE '[0-9.]+' | head -1)
echo "NetBridge presenter app audit"
echo "============================="
echo "  source   $VER"
echo "  bundle   $DIR"

sec "The bundle a person actually downloads"
[ -f "$APP" ]  && ok "NetBridgeSource present ($(du -h "$APP" | cut -f1))" \
               || { no "NetBridgeSource MISSING — nothing to run"; echo; exit 1; }
[ -x "$APP" ]  && ok "it is executable" || no "not executable — a double-click will fail"
# The mesh helper is a SEPARATE binary on purpose: PyInstaller strips a bundled Mach-O, so
# shipping it inside the app produced a helper that would not run. It must sit beside it.
[ -f "$MESH" ] && ok "netbridge-mesh sidecar present ($(du -h "$MESH" | cut -f1))" \
               || no "mesh sidecar MISSING — the app cannot reach a bridge without it"
[ -x "$MESH" ] && ok "sidecar is executable" || no "sidecar not executable"
if [ -f "$APP" ] && [ -f "$MESH" ]; then
  file "$APP" 2>/dev/null | grep -q "Mach-O" && ok "app is a native Mach-O binary" \
    || no "app is not a Mach-O executable"
  A=$(file "$APP" | grep -oE 'arm64|x86_64' | head -1)
  B=$(file "$MESH" | grep -oE 'arm64|x86_64' | head -1)
  [ "$A" = "$B" ] && ok "app and sidecar are the same architecture ($A)" \
    || no "architecture mismatch: app=$A sidecar=$B — the sidecar will not launch"
fi

note(){ printf "  \033[36mNOTE\033[0m  %s\n" "$1"; }
sec "Gatekeeper — the first thing that stops an amateur"
# The question is NOT "does the copy in my hand carry a quarantine flag". That flag is set by
# the DOWNLOADER, so a copy fetched with `gh release download` or curl never has one and this
# check went green while the real distribution path was broken. It is the same false green
# that has bitten this project repeatedly: testing the artefact you happen to hold instead of
# the one the user receives.
#
# What actually decides the outcome is the SIGNATURE. A browser download is always quarantined,
# and macOS kills a quarantined binary that is ad-hoc signed - SIGKILL, no dialog in a terminal,
# no output at all. Only a Developer ID signature plus notarisation survives that.
if [ -f "$APP" ]; then
  sig=$(codesign -dv "$APP" 2>&1)
  if printf '%s' "$sig" | grep -q 'flags=.*adhoc'; then
    no "ad-hoc signed: ANY browser download will be SIGKILLed by Gatekeeper.
        Verify with:  xattr -w com.apple.quarantine '0081;0;Safari;$(uuidgen)' <file> && ./<file>
        Needs a Developer ID signature + notarisation, or the recipient must run
        xattr -dr com.apple.quarantine <file> before first launch."
  elif printf '%s' "$sig" | grep -q 'TeamIdentifier=not set'; then
    no "no Team ID: not distributable — Gatekeeper will reject a downloaded copy"
  else
    ok "signed with a Team ID (survives a browser download)"
  fi

  # spctl is the system's own verdict on whether it would let this execute.
  if spctl -a -t execute "$APP" >/dev/null 2>&1; then
    ok "Gatekeeper accepts it for execution"
  else
    no "spctl REJECTS it — a recipient who downloads this in a browser cannot launch it"
  fi

  # Notarisation staple: what lets it pass with no network round-trip.
  if xcrun stapler validate "$APP" >/dev/null 2>&1; then
    ok "notarisation ticket is stapled"
  else
    warn "no stapled notarisation ticket (expected until an Apple Developer account is paid for)"
  fi

  if xattr -p com.apple.quarantine "$APP" >/dev/null 2>&1; then
    warn "this copy is quarantined (so it was fetched the way a real user would)"
  else
    note "this copy is NOT quarantined — it was fetched with a CLI, so it is NOT a fair test
        of the recipient experience; the signature checks above are what matter"
  fi
fi

sec "It actually runs, and brings its own tools"
# strings(1) cannot answer this. A PyInstaller one-file binary keeps its payload in a
# compressed archive, so grepping the executable finds neither ffmpeg nor GStreamer even when
# both are present - the first version of this audit warned about that and proved nothing.
# The only honest test is to run it and look at what it unpacked.
#
# Refuses to run if the app is already up: killing a live session to audit it would be a
# worse fault than any it could find.
if curl -s --max-time 3 http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
  warn "an app is already running on :8765 — skipping the launch test rather than disturbing it"
elif [ -x "$APP" ]; then
  ( cd "$DIR" && ./NetBridgeSource >/tmp/app-audit-launch.log 2>&1 & echo $! >/tmp/app-audit.pid )
  UP=""
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 3
    curl -s --max-time 3 http://127.0.0.1:8765/api/state >/dev/null 2>&1 && { UP=1; break; }
  done
  if [ -n "$UP" ]; then
    ok "launches and serves its UI within $((i*3))s (PyInstaller unpack included)"
    RV=$(curl -s --max-time 5 http://127.0.0.1:8765/api/state | tr -d '\r\n' \
         | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
    [ "$RV" = "$VER" ] && ok "running binary reports $RV, matching the source" \
      || no "binary reports '$RV' but the source says '$VER' — a stale build"
    for ep in /api/state /api/devices /api/bridges; do
      curl -s --max-time 6 "http://127.0.0.1:8765$ep" 2>/dev/null | grep -q "[{[]" \
        && ok "$ep responds" || no "$ep does not respond"
    done
    MEI=$(ps -Ao args | grep -m1 "[_]MEI" | grep -oE "/var/folders/[^ ]*/_MEI[A-Za-z0-9]*" | head -1)
    if [ -n "$MEI" ] && [ -d "$MEI" ]; then
      [ -x "$MEI/ffmpeg" ] && ok "ffmpeg IS bundled (unpacked at $(basename "$MEI")/ffmpeg)" \
        || no "no ffmpeg in the bundle — going live would fail on a clean machine"
      ls "$MEI"/gst/gst-launch-1.0 >/dev/null 2>&1 \
        && ok "GStreamer IS bundled (room audio needs it)" \
        || no "no GStreamer in the bundle — return audio would fall back or fail"
    else
      warn "could not locate the unpack directory; bundled tools unverified"
    fi
    # Put the machine back exactly as it was found.
    kill -TERM "$(cat /tmp/app-audit.pid 2>/dev/null)" 2>/dev/null; sleep 5
    pgrep -f NetBridgeSource >/dev/null && { sleep 4; pkill -TERM -f NetBridgeSource; } || true
    pkill -TERM -f netbridge-mesh 2>/dev/null; sleep 2
    pgrep -f "NetBridgeSource|netbridge-mesh" >/dev/null \
      && warn "left a process running after the audit — check manually" \
      || ok "stopped cleanly after the test (nothing left holding the camera)"
    [ "$(lsof 2>/dev/null | grep -icE 'AppleH1[0-9]CamIn')" = "0" ] \
      && ok "camera released — the audit did not wedge anything" \
      || no "something is still holding the camera after shutdown"
  else
    no "did NOT start within 36s — an amateur would see nothing happen"
    sed -n '1,12p' /tmp/app-audit-launch.log 2>/dev/null | sed 's/^/        /'
    pkill -f NetBridgeSource 2>/dev/null
  fi
fi

sec "The fixes that came out of real failures"
# --- the camera wedge (2026-08-14)
srcgrep 'stdin=subprocess\.PIPE' \
  && ok "media legs get a stdin pipe, so ffmpeg can be asked to quit" \
  || no "no stdin pipe — the only way to stop ffmpeg is a signal, which wedges the camera"
Q=$(body _quit)
A=$(printf '%s' "$Q" | grep -n 'b"q'          | head -1 | cut -d: -f1)
B=$(printf '%s' "$Q" | grep -n '\.terminate()' | head -1 | cut -d: -f1)
C=$(printf '%s' "$Q" | grep -n '\.kill()'      | head -1 | cut -d: -f1)
if [ -n "$A" ] && [ -n "$B" ] && [ -n "$C" ]; then
  if [ "$A" -lt "$B" ] && [ "$B" -lt "$C" ]; then
    ok "shutdown escalates ask -> SIGTERM -> SIGKILL (never straight to kill)"
  else no "shutdown order wrong — a SIGKILL can reach ffmpeg while it holds the camera"; fi
else no "_quit() escalation not found"; fi
srcgrep 'def _quit' && ok "_quit() helper exists" || no "_quit() missing"
body respawn_leg | grep -q '_quit(' \
  && ok "single-leg repair releases the device too (a repair is the worst time to wedge it)" \
  || no "respawn_leg still kills without releasing"

# --- the misdiagnosis (2026-08-14)
srcgrep 'def _giving_up_because' \
  && ok "supervisor decides WHICH END is at fault before advising" \
  || no "supervisor still says 'check the bridge' for every failure"
srcgrep 'fix-camera-macos\.sh' \
  && ok "names the actual repair command for a wedged camera" \
  || no "no repair command offered for the most common local failure"

# --- teardown on every exit path
srcgrep 'SIGTERM' && srcgrep 'SIGHUP' \
  && ok "cleans up on SIGTERM/SIGHUP too, not only Ctrl-C" \
  || no "closing the terminal would leave the camera held"
srcgrep '_kill_orphan_media' \
  && ok "sweeps its own leftovers at startup after a hard kill" \
  || warn "no orphan sweep — a crashed run could leave ffmpeg holding the camera"

sec "Ending a session actually ends it"
# The camera kept recording after an operator ended a session, because `live` was derived
# from process state - so a stop and a crash looked identical and the supervisors restarted
# the legs. Intent has to be recorded, not inferred.
srcgrep 'wanted = False' && ok "the session records whether it is WANTED, not just running" \
  || no "no intent flag — a stop is indistinguishable from a crash"
if body stop | grep -q 'self.wanted = False'; then
  ST=$(body stop)
  IF=$(printf '%s' "$ST" | grep -n 'self.wanted = False' | head -1 | cut -d: -f1)
  IK=$(printf '%s' "$ST" | grep -n '_quit(\|stdin.write' | head -1 | cut -d: -f1)
  if [ -z "$IK" ] || [ "$IF" -lt "$IK" ]; then
    ok "intent is recorded BEFORE anything is killed (no window for a tick to resurrect it)"
  else no "killed first, flagged second — a supervisor tick in between restarts the session"; fi
else no "stop() does not record intent"; fi
body respawn_leg | grep -q 'wanted' \
  && ok "leg repair refuses on a session the operator ended" \
  || no "the supervisor will silently undo a deliberate stop"
body set_return | grep -q 'wanted' \
  && ok "the return player will not restart on an ended session" \
  || no "the return player can resurrect `live` after a stop"

sec "Return audio uses the redundancy the bridge already pays for"
# The bridge encodes with inband-fec=true packet-loss-percentage=20. One flag used to switch
# do-lost, use-inband-fec and plc together and was off because PLC's guesses were audible -
# so a fifth of the bridge's bitrate was redundancy this machine discarded. FEC rebuilds from
# data actually sent; PLC invents. They are different questions.
srcgrep 'return_fec' && srcgrep 'return_plc' \
  && ok "FEC and PLC are separate switches" \
  || no "one flag still decides both — turning off the guessing also loses the repair"
srcgrep 'NB_RETURN_FEC", "1"' && ok "FEC defaults ON (the redundancy is already on the wire)" \
  || warn "FEC does not default on"
srcgrep 'NB_RETURN_PLC", "0"' && ok "PLC defaults OFF (its guesses were the artifact)" \
  || warn "PLC does not default off"

sec "Safety of what it accepts from the network"
# The bridge is a courier for presenter tuning. A courier must not be able to make this
# machine do something unbounded.
srcgrep 'def set_return_tuning' && ok "return tuning goes through one setter" \
  || warn "tuning applied without a single choke point"
if body set_return_tuning | grep -qE 'max\(.*min\(' ; then
  ok "tuning values are clamped before use (a bridge cannot set an unbounded buffer)"
else
  no "tuning from the bridge is NOT clamped"
fi
srcgrep 'APP_VERSION' && ok "carries a version stamp the updater can compare" || no "no version stamp"
grep -q 'refuses unsigned' "$HERE/app/netbridge-source/build.py" 2>/dev/null \
  && ok "updater refuses unsigned updates by design" \
  || warn "could not confirm the updater rejects unsigned manifests"

sec "Nothing secret is inside the binary"
if [ -f "$APP" ]; then
  for pat in 'tskey-auth-[a-zA-Z0-9]{10}' 'AKIA[0-9A-Z]{16}' 'ghp_[A-Za-z0-9]{20}'; do
    if strings "$APP" 2>/dev/null | grep -qE "$pat"; then no "a credential matching /$pat/ is baked into the app"
    else ok "no credential matching /$pat/"; fi
  done
fi

echo
# A check that errors out (typo'd helper, missing tool) must never be counted as a pass. This
# script printed "0 failed - the bundle is sound" while two checks died on a command-not-found;
# an audit that cannot fail loudly is worth nothing.
if [ "$PASS" -eq 0 ]; then no "no checks executed at all — the audit itself is broken"; fi
printf '\033[1m  %d passed, %d failed, %d warnings\033[0m\n' "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -eq 0 ] && echo "  → the bundle is sound" || echo "  → DO NOT SHIP"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
