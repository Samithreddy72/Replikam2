#!/usr/bin/env bash
# Run the whole suite, and treat a test that produces NO RESULT as a failure.
#
# WHY THIS EXISTS
# ---------------
# tests/test-app-diagnosis.py crashed with a NameError for two whole phases and nobody noticed.
# It died BEFORE printing its "N passed, M failed" line, so the ad-hoc loops being used to run
# the suite saw no numbers, added zero to the total, and moved on. The suite reported a healthy
# 346 passing while one of its files had not executed a single assertion since the code it
# tests was changed underneath it.
#
# A test that cannot fail loudly is not a test. So: no summary line, or a non-zero exit, is a
# FAILURE here - never a blank row.
#
# Tests whose dependencies are genuinely absent may SKIP, but they must say so in their own
# output; a skip is reported and counted separately, never folded into the pass total.
#
#   bash tools/run-tests.sh            # everything
#   bash tools/run-tests.sh --python /tmp/bev/bin/python   # include backend tests
set -uo pipefail
cd "$(dirname "$0")/.."

PY="python3"
MEDIA_PY=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --python|--media-python)
      [ "$#" -ge 2 ] || { echo "missing interpreter after $1" >&2; exit 2; }
      if [ "$1" = "--python" ]; then PY="$2"; else MEDIA_PY="$2"; fi
      shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
MEDIA_PY="${MEDIA_PY:-$PY}"

PASS=0; FAIL=0; SKIP=0; BROKEN=0
FAILED_FILES=""

for t in tests/test-*.py tests/test-*.sh; do
  [ -e "$t" ] || continue
  name=$(basename "$t")
  skips_before=$SKIP
  test_py="$PY"
  # PyGObject is tied to the system GStreamer Python ABI; the backend may need a
  # different supported interpreter. Both runtimes remain explicit in the release run.
  [ "$name" = "test-audio-engine.py" ] && test_py="$MEDIA_PY"
  case "$t" in
    *.py) out=$("$test_py" "$t" 2>&1); rc=$? ;;
    *)    out=$(bash "$t" 2>&1);  rc=$? ;;
  esac

  if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qE "^[[:space:]]*SKIPPED([[:space:]]|$)"; then
    SKIP=$((SKIP+1))
    printf '  %-32s \033[33mSKIPPED\033[0m  (dependencies absent)\n' "$name"
    continue
  fi

  line=$(printf '%s' "$out" | grep -oE "[0-9]+ passed, [0-9]+ failed" | tail -1)
  if [ -n "$line" ]; then
    custom_skips=$(printf '%s' "$out" | grep -E "[0-9]+ passed, [0-9]+ failed" | tail -1 | grep -oE '[0-9]+ skipped' | grep -oE '^[0-9]+' || true)
    SKIP=$((SKIP + ${custom_skips:-0}))
    if printf '%s' "$out" | grep -qE 'SKIPPED.*dependencies absent|SKIP .*not installed'; then
      SKIP=$((SKIP+1))
    fi
  fi
  # unittest files end with their own summary: "Ran N tests ..." then "OK" or "FAILED (failures=F, errors=E)".
  # That IS a result, so read it rather than calling the file broken.
  if [ -z "$line" ] && ran=$(printf '%s' "$out" | grep -oE "^Ran [0-9]+ tests?" | tail -1 | grep -oE "[0-9]+"); then
    if printf '%s' "$out" | grep -qE "^OK( |$)"; then
      sk=$(printf '%s' "$out" | grep -oE "^OK \(.*skipped=[0-9]+" | grep -oE "skipped=[0-9]+" | grep -oE "[0-9]+" || true)
      SKIP=$((SKIP + ${sk:-0}))
      line="$(( ran - ${sk:-0} )) passed, 0 failed"
    elif fl=$(printf '%s' "$out" | grep -E "^FAILED \("); then
      nf=$(printf '%s' "$fl" | grep -oE "(failures|errors)=[0-9]+" | grep -oE "[0-9]+" | paste -sd+ - | bc)
      sk=$(printf '%s' "$fl" | grep -oE "skipped=[0-9]+" | grep -oE "[0-9]+" || true)
      SKIP=$((SKIP + ${sk:-0}))
      line="$(( ran - nf - ${sk:-0} )) passed, $nf failed"
    fi
  fi
  if [ -z "$line" ]; then
    # No summary at all: the file crashed, exited early, or never reached its own footer.
    # This is the case that hid a broken test for two phases.
    BROKEN=$((BROKEN+1)); FAILED_FILES="$FAILED_FILES $name"
    printf '  %-32s \033[31mNO RESULT\033[0m  (crashed or exited early, rc=%s)\n' "$name" "$rc"
    printf '%s\n' "$out" | tail -4 | sed 's/^/        /'
    continue
  fi

  p=$(printf '%s' "$line" | grep -oE '^[0-9]+')
  f=$(printf '%s' "$line" | grep -oE '[0-9]+ failed' | grep -oE '^[0-9]+')
  PASS=$((PASS + p)); FAIL=$((FAIL + f))
  # A success-looking footer must never override a process failure.
  if [ "$rc" -ne 0 ] && [ "$f" -eq 0 ]; then
    BROKEN=$((BROKEN+1))
  fi
  if [ "$f" -gt 0 ] || [ "$rc" -ne 0 ]; then
    FAILED_FILES="$FAILED_FILES $name"
    printf '  %-32s \033[31m%s\033[0m  (rc=%s)\n' "$name" "$line" "$rc"
  else
    printf '  %-32s %s (%s skipped)\n' "$name" "$line" "$((SKIP-skips_before))"
  fi
done

echo "  ────────────────────────────────────────────────────"
printf '  \033[1mTOTAL %d passed, %d failed, %d skipped, %d with NO RESULT\033[0m\n' \
       "$PASS" "$FAIL" "$SKIP" "$BROKEN"
[ -n "$FAILED_FILES" ] && echo "  needs attention:$FAILED_FILES"
[ "$FAIL" -eq 0 ] && [ "$BROKEN" -eq 0 ] && [ "$SKIP" -eq 0 ] && echo "  → suite is green" || echo "  → SUITE NOT GREEN"
exit $([ "$FAIL" -eq 0 ] && [ "$BROKEN" -eq 0 ] && [ "$SKIP" -eq 0 ] && echo 0 || echo 1)
