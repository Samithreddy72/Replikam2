#!/bin/bash
# Do the bridge's GStreamer pipelines actually LINK?
#
# WHY THIS EXISTS
# ---------------
# On 2026-08-12 a freshly flashed card could not send the presenter's voice. The cause was
# one word in bridge-feeder-audio.sh:
#
#     ... audio/x-raw,rate=48000,channels=2,format=S16LE ! rtpL16pay ...
#
# RTP L16 is big-endian by RFC 3551, so rtpL16pay accepts S16BE and nothing else. It refused
# to link, and because gst-launch builds the WHOLE pipeline or none of it, an optional
# echo-cancellation branch took the live audio path down with it. systemd respawned the
# failure every two seconds; the restart counter reached 16 before anyone noticed.
#
# The code had been committed for weeks and never run — the previous card carried an older
# script. Every audit before it checked that the file PARSED as shell, that the systemd unit
# pointed at a real binary, and that the element names were present. All true. All useless:
# `bash -n` cannot know that two GStreamer elements refuse to speak to each other.
#
#   bash tests/test-gst-pipelines.sh
#
#
# HOW IT WORKS, AND WHY THE FIRST DESIGN WAS ABANDONED
# ----------------------------------------------------
# Version 1 recovered each pipeline by grepping the script for a `gst-launch-1.0 ...` line
# and textually substituting shell variables. That could not work, and it failed in the
# worst possible way — silently.
#
# bridge-return-audio.sh builds its pipeline out of SEVEN shell variables:
#
#     "$GST" ${aec_probe:+$aec_probe} alsasrc ... ${RETURN_SRC_PROPS:-} \
#       ! audioconvert $pre! audioresample quality=10 \
#       ! ${aec_filter}opusenc ...
#
# No regex reconstructs that. So the harness reported FAIL on the one file whose pipeline
# was genuinely broken (`! ${aec_filter}` expanded to `! !` whenever echo cancellation was
# off, which is always), and the failure was written off as "a known false alarm the
# hardware disproves". A permanent FAIL that everyone is trained to ignore is worse than no
# test at all: it is where the next real regression will hide.
#
# Version 2 does not parse anything. It RUNS each real script with a stub `gst-launch-1.0`
# that records the argv it was called with, so the SHELL performs every expansion — exactly
# as it does on the Pi. Whatever comes out is what systemd would really execute. That argv
# is then handed to a real gst-launch-1.0 with hardware elements swapped for fakes.
#
# Consequences worth knowing:
#   - `! !` from an empty variable is caught in the captured argv, so it is detected even on
#     a machine that lacks the plugins (this is the 2026-08-12 return-audio bug).
#   - Every branch is covered, including AEC on/off and the no-kernel-control fail-safe,
#     which is a DIFFERENT pipeline that the old harness never saw at all.
#   - An element missing locally (webrtcdsp lives in plugins-bad) is reported SKIP, never
#     PASS — a check that silently passes because it could not run is the exact failure this
#     file exists to prevent.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

command -v gst-launch-1.0 >/dev/null 2>&1 || {
  echo "SKIP: gst-launch-1.0 not installed — cannot verify pipelines"; exit 0; }

T="$(mktemp -d)"
KIDS=()
cleanup(){ for p in "${KIDS[@]:-}"; do kill -9 "$p" 2>/dev/null; done; rm -rf "$T"; }
trap cleanup EXIT

PASS=0; FAIL=0; SKIP=0
ok(){   echo "  PASS  $1"; PASS=$((PASS+1)); }
no(){   echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
skip(){ echo "  SKIP  $1"; SKIP=$((SKIP+1)); }

# Portable bounded run. `timeout` is GNU coreutils and is NOT on stock macOS — the first
# version of this file used it, so every gst-launch invocation died with "command not found",
# produced no output, matched no error pattern, and the whole suite reported PASS. Including
# the negative control, which is meant to prove the check works at all. A test that cannot
# run must never look like a test that passed, which is the entire point of this file.
run_bounded() {                 # run_bounded <seconds> <cmd...>
  local secs="$1"; shift
  "$@" >"$T/out" 2>&1 </dev/null &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  local killer=$!
  wait "$pid" 2>/dev/null
  kill -9 "$killer" 2>/dev/null
  cat "$T/out"
}

# ---------------------------------------------------------------------------- the stubs
mkdir -p "$T/bin"
# The recorder. Writes the argv it was handed, one invocation per line, then exits 0 so the
# calling script proceeds normally instead of hanging.
cat > "$T/bin/gst-launch-1.0" <<'STUB'
#!/bin/bash
echo "$@" >> "$ARGV_LOG"
exit 0
STUB
# Reports a plausible host rate so bridge-return-audio.sh takes its FOLLOWER path. Output
# mirrors real amixer, including the type line whose `values=1` is a COUNT — a minimal stub
# once let a parser bug reach hardware, so the stub stays realistic.
cat > "$T/bin/amixer" <<'STUB'
#!/bin/bash
case " $* " in *" controls "*) echo "numid=5,iface=PCM,name='Capture Rate'"; exit 0;; esac
[ -f "$T_HOSTRATE" ] || exit 1          # simulates "control does not exist"
echo "numid=5,iface=PCM,name='Capture Rate'"
echo "  ; type=INTEGER,access=r--v----,values=1,min=0,max=192000,step=0"
echo "  : values=$(cat "$T_HOSTRATE")"
STUB
cat > "$T/bin/alsactl" <<'STUB'
#!/bin/bash
exec sleep 600
STUB
chmod +x "$T/bin"/*
export ARGV_LOG="$T/argv.log" T_HOSTRATE="$T/hostrate"

# The two feeder scripts pin `export PATH=/usr/local/bin:/usr/bin:/bin` for systemd
# determinism and then `exec gst-launch-1.0` — so prefixing PATH from out here is discarded
# by the script itself. Copy the script and rewrite ONLY that one export line. The pipeline
# text, which is what is under test, is untouched and the substitution is asserted below.
stage() {                       # stage <script> -> prints staged path
  local src="$1" dst="$T/$(basename "$1")"
  sed "s#^export PATH=#export PATH=$T/bin:#" "$src" > "$dst"
  chmod +x "$dst"
  grep -q "^export PATH=$T/bin:" "$dst" || { echo "STAGE-FAILED"; return 1; }
  printf '%s' "$dst"
}

# Run a staged script until it calls the stub, and return the argv it built.
capture() {                     # capture <staged> [env assignments...]
  : > "$ARGV_LOG"
  local staged="$1"; shift
  env "$@" bash "$staged" >>"$T/script.log" 2>&1 &
  local pid=$!; KIDS+=("$pid")
  # Detach from job control before killing: the scripts are supervisors that never exit on
  # their own, so they are always killed, and a reaped SIGKILL prints "Killed: 9" to stderr
  # that reads like a test failure in the output.
  disown "$pid" 2>/dev/null
  for _ in $(seq 1 40); do
    [ -s "$ARGV_LOG" ] && break
    sleep 0.1
  done
  kill -9 "$pid" 2>/dev/null
  head -1 "$ARGV_LOG"
}

# Swap the elements that need a Pi. Applied to the CAPTURED argv, so what is checked is the
# real command with only the hardware removed. Properties are consumed along with the
# element name: `fakesink device=plughw:...` is an unknown-property error, not a link test.
defake() {
  # Replace an element's own property=value words only. The first version ate everything up to
  # the next '!', which for a tee branch includes the NEXT branch's pad name ("... spk.") and
  # linked the fake sink into the following queue - a failure of the test, not the pipeline.
  sed -E \
    -e 's#alsasink( +[a-z-]+=[^ !]+)*#fakesink#g' \
    -e 's#alsasrc( +[a-z-]+=[^ !]+)*#audiotestsrc num-buffers=1#g' \
    -e 's#v4l2sink( +[a-z-]+=[^ !]+)*#fakesink#g' \
    -e 's#v4l2src( +[a-z-]+=[^ !]+)*#videotestsrc num-buffers=1#g' \
    -e 's#udpsink( +[a-z-]+=[^ !]+)*#fakesink#g'
}

# Construct a pipeline for real and judge the result.
check() {                       # check <label> <pipeline string>
  local label="$1" pipe="$2" out
  # An empty shell variable between two separators yields "! !". gst reports that only as a
  # vague "syntax error", and it is invisible to a machine that lacks the plugins — so catch
  # it in the argv, where the message can name the cause. This is the 2026-08-12 bug.
  if grep -qE '![[:space:]]*!' <<<"$pipe"; then
    no "$label — empty variable leaves '! !' in the pipeline"
    echo "          $(grep -oE '.{0,40}![[:space:]]*!.{0,40}' <<<"$pipe" | head -1)"
    return
  fi
  # shellcheck disable=SC2086
  out=$(run_bounded 8 gst-launch-1.0 $pipe)
  # ORDER MATTERS. A missing plugin reports as `erroneous pipeline: no element "webrtcdsp"`,
  # which also matches the link-error pattern — so the absent-element case must be tested
  # FIRST or a plugin this machine simply lacks is reported as a broken pipeline.
  if grep -qiE "no element \"|no such element" <<<"$out"; then
    skip "$label  ($(grep -oiE 'no element \"[^\"]+\"' <<<"$out" | head -1) — present on the Pi, not here)"
    return
  fi
  if grep -qiE "could not link|erroneous pipeline|not handle caps|syntax error" <<<"$out"; then
    no "$label"
    grep -iE "could not link|erroneous pipeline|not handle caps|syntax error" <<<"$out" \
      | head -2 | sed 's/^/          /'
    return
  fi
  ok "$label"
}

echo
echo "GStreamer pipeline link check"
echo "============================="
echo "  (pipelines are captured from the real scripts, not parsed out of them)"
echo

# ---- the exact chain that broke, element for element ------------------------------------
# Kept as its own case so a regression names itself instead of hiding inside a full pipeline.
check "L16 reference branch (the 2026-08-12 regression)" \
  "audiotestsrc num-buffers=1 ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2,format=S16BE ! rtpL16pay ! fakesink"

# And prove the test can actually SEE the bug: the old, broken caps must fail.
echo "  ---- negative controls: these MUST fail, or the suite proves nothing ----"
neg=$(run_bounded 8 gst-launch-1.0 audiotestsrc num-buffers=1 ! audioconvert ! audioresample \
      ! audio/x-raw,rate=48000,channels=2,format=S16LE ! rtpL16pay ! fakesink)
if grep -qiE "could not link|not handle caps" <<<"$neg"; then
  ok "detects the S16LE endianness bug"
else
  no "did NOT detect the known S16LE bug — this suite proves nothing"
fi
# The second negative control covers the OTHER 2026-08-12 bug, and it is the one the old
# harness structurally could not see.
if grep -qE '![[:space:]]*!' <<<"audioconvert ! ! opusenc"; then
  ok "detects the empty-variable '! !' bug"
else
  no "did NOT detect the '! !' bug — this suite proves nothing"
fi
echo

# ---- every real pipeline, captured from the real script ---------------------------------
echo "  ---- captured from the shipped scripts ----"

VIDEO=$(python3 -c 'import runpy; print(runpy.run_path("pi/scripts/bridge-video-receiver.py")["pipeline_description"](100))')
check "bridge-video-receiver.py (video)" "$(printf '%s' "$VIDEO" | defake)"

S=$(stage pi/scripts/bridge-feeder-audio.sh)
if [ "$S" = "STAGE-FAILED" ] || [ -z "$S" ]; then
  no "bridge-feeder-audio.sh — could not stage"
else
  # AEC on: the reference branch is built and must be S16BE.
  FA="$(capture "$S" RETURN_AEC=1)"
  # Plugin-INDEPENDENT assertion on the captured argv. The full construction below SKIPs on
  # a machine without webrtcdsp (macOS), and a skip must never be the only thing standing
  # between this project and the exact regression that took the audio down. Endianness is
  # checkable as text, so check it as text, everywhere, always.
  if grep -qE 'format=S16BE +! *rtpL16pay' <<<"$FA"; then
    ok "bridge-feeder-audio.sh — L16 reference branch is S16BE (checked in argv)"
  else
    no "bridge-feeder-audio.sh — L16 reference branch is NOT S16BE; rtpL16pay will refuse to link"
    echo "          $(grep -oE '.{0,30}rtpL16pay.{0,10}' <<<"$FA" | head -1)"
  fi
  check "bridge-feeder-audio.sh AEC on (presenter voice + L16 reference)" "$(defake <<<"$FA")"
  # AEC off (the shipped default): nothing listens on the reference port, so the branch must
  # not be built at all - and the voice path must still link on its own.
  FA0="$(capture "$S" RETURN_AEC=0)"
  if [ -n "$FA0" ] && ! grep -q 'rtpL16pay' <<<"$FA0" && grep -q 'alsasink' <<<"$FA0"; then
    ok "bridge-feeder-audio.sh — AEC off builds no reference branch (voice path only)"
  else
    no "bridge-feeder-audio.sh — AEC off still builds the reference branch (or no voice path)"
  fi
  check "bridge-feeder-audio.sh AEC off (presenter voice only)" "$(defake <<<"$FA0")"
fi

# bridge-return-audio.sh has THREE distinct pipelines. The old harness saw none of them.
S=$(stage pi/scripts/bridge-return-audio.sh)
if [ "$S" = "STAGE-FAILED" ] || [ -z "$S" ]; then
  no "bridge-return-audio.sh — could not stage"
else
  # 1. follower path, AEC off — the default, and where the '! !' bug lived.
  echo 48000 > "$T_HOSTRATE"
  P=$(capture "$S" RETURN_DEST_IP=10.0.0.1 RETURN_RUNDIR="$T/r1" RETURN_DEBOUNCE_S=0 | defake)
  [ -n "$P" ] && check "bridge-return-audio.sh — follower, AEC off (the default)" "$P" \
               || no "bridge-return-audio.sh — follower AEC off: captured nothing"

  # 2. follower path, AEC on — the branch the deferred echo-cancellation work will switch on.
  #    It was broken too: aec_filter already ends in '!', so '! ${aec_filter}' gave '! ... ! !'.
  P=$(capture "$S" RETURN_DEST_IP=10.0.0.1 RETURN_RUNDIR="$T/r2" RETURN_DEBOUNCE_S=0 RETURN_AEC=1 | defake)
  [ -n "$P" ] && check "bridge-return-audio.sh — follower, AEC on (deferred feature)" "$P" \
               || no "bridge-return-audio.sh — follower AEC on: captured nothing"

  # 3. fail-safe path — taken when the kernel has no Capture Rate control. A completely
  #    separate pipeline, and the one that runs when rate following is unavailable.
  rm -f "$T_HOSTRATE"
  P=$(capture "$S" RETURN_DEST_IP=10.0.0.1 RETURN_RUNDIR="$T/r3" RETURN_DEBOUNCE_S=0 | defake)
  [ -n "$P" ] && check "bridge-return-audio.sh — fail-safe, no kernel rate control" "$P" \
               || no "bridge-return-audio.sh — fail-safe: captured nothing"
fi

echo
echo "  $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ] || { echo "  A pipeline that cannot link takes the WHOLE service down."; exit 1; }
exit 0
