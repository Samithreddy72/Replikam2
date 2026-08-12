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
# So this test constructs each pipeline for real and fails on a link error. It needs no Pi:
# linking is negotiated at parse time, before any hardware is touched.
#
#   bash tests/test-gst-pipelines.sh
#
# Hardware sinks/sources are swapped for fakesink/fakesrc so it runs anywhere. An element
# missing locally (webrtcdsp lives in plugins-bad) is reported SKIP, never PASS — a check
# that silently passes because it could not run is the exact failure this file is here to
# prevent.
set -uo pipefail
cd "$(dirname "$0")/.."

command -v gst-launch-1.0 >/dev/null 2>&1 || {
  echo "SKIP: gst-launch-1.0 not installed — cannot verify pipelines"; exit 0; }

# Portable bounded run. `timeout` is GNU coreutils and is NOT on stock macOS — the first
# version of this file used it, so every gst-launch invocation died with "command not found",
# produced no output, matched no error pattern, and the whole suite reported PASS. Including
# the negative control, which is meant to prove the check works at all. A test that cannot
# run must never look like a test that passed, which is the entire point of this file.
run_bounded() {                 # run_bounded <seconds> <cmd...>
  local secs="$1"; shift
  "$@" >/tmp/.gstchk.out 2>&1 </dev/null &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  local killer=$!
  wait "$pid" 2>/dev/null
  kill -9 "$killer" 2>/dev/null
  cat /tmp/.gstchk.out
}

PASS=0; FAIL=0; SKIP=0
check() {                       # check <label> <pipeline...>
  local label="$1"; shift
  local out
  out=$(run_bounded 6 gst-launch-1.0 "$@")

  # ORDER MATTERS. A missing plugin reports as `erroneous pipeline: no element "webrtcdsp"`,
  # which also matches the link-error pattern — so the absent-element case must be tested
  # FIRST or a plugin this machine simply lacks is reported as a broken pipeline.
  if grep -qiE "no element \"|no such element" <<<"$out"; then
    echo "  SKIP  $label  ($(grep -oiE 'no element \"[^\"]+\"' <<<"$out" | head -1) — present on the Pi, not here)"
    SKIP=$((SKIP+1)); return
  fi
  if grep -qiE "could not link|erroneous pipeline|not handle caps" <<<"$out"; then
    echo "  FAIL  $label"
    grep -iE "could not link|erroneous pipeline|not handle caps" <<<"$out" \
      | head -2 | sed 's/^/          /'
    FAIL=$((FAIL+1)); return
  fi
  echo "  PASS  $label"; PASS=$((PASS+1))
}

echo
echo "GStreamer pipeline link check"
echo "============================="

# ---- the exact chain that broke, element for element ------------------------------------
# The reference branch alone, which is where the fault was. Kept as its own case so a
# regression names itself instead of hiding inside the full pipeline.
check "L16 reference branch (the 2026-08-12 regression)" \
  audiotestsrc num-buffers=1 ! audioconvert ! audioresample \
  ! audio/x-raw,rate=48000,channels=2,format=S16BE ! rtpL16pay ! fakesink

# And prove the test can actually SEE the bug: the old, broken caps must fail.
echo "  ---- negative control: the broken version MUST fail ----"
neg=$(run_bounded 6 gst-launch-1.0 audiotestsrc num-buffers=1 ! audioconvert ! audioresample \
      ! audio/x-raw,rate=48000,channels=2,format=S16LE ! rtpL16pay ! fakesink)
if grep -qiE "could not link|not handle caps" <<<"$neg"; then
  echo "  PASS  the check detects the S16LE bug"; PASS=$((PASS+1))
else
  echo "  FAIL  the check did NOT detect the known bug — it proves nothing"; FAIL=$((FAIL+1))
fi

# ---- every real pipeline, with hardware swapped out --------------------------------------
# Extract each gst-launch invocation from the shipped scripts, substitute the elements that
# need a Pi, and construct it. Substitutions are deliberately narrow: swapping more than the
# hardware would test a pipeline nobody runs.
for f in pi/scripts/bridge-feeder-audio.sh pi/scripts/bridge-return-audio.sh \
         pi/scripts/bridge-feeder-net.sh; do
  [ -f "$f" ] || continue
  pipe=$(tr '\n' ' ' < "$f" | grep -oE 'gst-launch-1\.0 .*' | head -1)
  [ -n "$pipe" ] || { echo "  SKIP  $(basename "$f") (no gst-launch line found)"; SKIP=$((SKIP+1)); continue; }
  pipe=${pipe#gst-launch-1.0 }
  pipe=$(sed -E \
      -e 's#alsasink[^!]*#fakesink #g' \
      -e 's#alsasrc[^!]*#audiotestsrc num-buffers=1 #g' \
      -e 's#v4l2sink[^!]*#fakesink #g' \
      -e 's#v4l2src[^!]*#videotestsrc num-buffers=1 #g' \
      -e 's#osxaudiosink[^!]*#fakesink #g' \
      -e 's#\$\{[A-Z_]+:-([^}]*)\}#\1#g' \
      -e 's#\$\{[A-Z_]+\}##g' <<<"$pipe")
  # shellcheck disable=SC2086
  check "$(basename "$f")" $pipe
done

echo
echo "  $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ] || { echo "  A pipeline that cannot link takes the WHOLE service down."; exit 1; }
exit 0
