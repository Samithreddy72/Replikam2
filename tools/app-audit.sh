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

sec "Gatekeeper — the first thing that stops an amateur"
if [ -f "$APP" ]; then
  if xattr -p com.apple.quarantine "$APP" >/dev/null 2>&1; then
    warn "quarantine flag set — first launch shows 'unidentified developer'; the recipient must
        right-click -> Open once, or run: xattr -dr com.apple.quarantine <file>"
  else
    ok "no quarantine flag on this copy — it will launch without the Gatekeeper prompt"
  fi
  codesign -dv "$APP" >/dev/null 2>&1 && ok "carries a code signature" \
    || warn "not code-signed (expected; signing needs a paid Apple account)"
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
printf '\033[1m  %d passed, %d failed, %d warnings\033[0m\n' "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -eq 0 ] && echo "  → the bundle is sound" || echo "  → DO NOT SHIP"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
