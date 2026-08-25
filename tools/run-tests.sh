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
[ "${1:-}" = "--python" ] && { PY="$2"; shift 2; }

PASS=0; FAIL=0; SKIP=0; BROKEN=0
FAILED_FILES=""

for t in tests/test-*.py tests/test-*.sh; do
  [ -e "$t" ] || continue
  name=$(basename "$t")
  case "$t" in
    *.py) out=$("$PY" "$t" 2>&1); rc=$? ;;
    *)    out=$(bash "$t" 2>&1);  rc=$? ;;
  esac

  if printf '%s' "$out" | grep -q "SKIPPED"; then
    SKIP=$((SKIP+1))
    printf '  %-32s \033[33mSKIPPED\033[0m  (dependencies absent)\n' "$name"
    continue
  fi

  line=$(printf '%s' "$out" | grep -oE "[0-9]+ passed, [0-9]+ failed" | tail -1)
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
  if [ "$f" -gt 0 ] || [ "$rc" -ne 0 ]; then
    FAILED_FILES="$FAILED_FILES $name"
    printf '  %-32s \033[31m%s\033[0m  (rc=%s)\n' "$name" "$line" "$rc"
  else
    printf '  %-32s %s\n' "$name" "$line"
  fi
done

echo "  ────────────────────────────────────────────────────"
printf '  \033[1mTOTAL %d passed, %d failed, %d skipped, %d with NO RESULT\033[0m\n' \
       "$PASS" "$FAIL" "$SKIP" "$BROKEN"
[ -n "$FAILED_FILES" ] && echo "  needs attention:$FAILED_FILES"
[ "$FAIL" -eq 0 ] && [ "$BROKEN" -eq 0 ] && echo "  → suite is green" || echo "  → SUITE NOT GREEN"
exit $([ "$FAIL" -eq 0 ] && [ "$BROKEN" -eq 0 ] && echo 0 || echo 1)
