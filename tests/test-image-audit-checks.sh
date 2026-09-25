#!/usr/bin/env bash
# The image audit's checks must read CODE, not comments (2026-09-25).
#
# Image 2.2.0 failed "A/B health check still waits for fleet-brain" on the comment in bridge-ab that
# explains fleet-brain had been REMOVED, and a comment-blind re-run of the audit showed two more
# checks passing on comments alone: the setup-AP password (the value is enforced in
# bridge-derive-pass; the portal only mentions it in a comment) and the PIN count-first order
# (the check looked for the words "COUNT FIRST"). Each check below is taken from the audit's own
# text and run against the real file and against copies broken on purpose, so a check that cannot
# fail, or that fails on a comment, shows up here instead of at the next image.
#
#   bash tests/test-image-audit-checks.sh
set -uo pipefail
cd "$(dirname "$0")/.."
A=tools/image-audit.sh
CAT=$(mktemp -d); trap 'rm -rf "$CAT"' EXIT
passed=0; failed=0
RESULT=""
ok(){ RESULT=PASS; }; no(){ RESULT=FAIL; }; warn(){ RESULT=WARN; }

eval "$(grep -E '^grepf\(\)' "$A")"
eval "$(sed -n '/^count_first() {/,/^}/p' "$A")"
AB=$(sed -n "/^grepf bridge-ab 'fleet-brain'/,/does not require fleet contact\"; }/p" "$A")
WIFI=$(sed -n '/^grepf bridge-derive-pass/,/check before shipping a card/p' "$A")
PINC=$(sed -n '/^count_first && ok/,/unlimited guesses"/p' "$A")

lines(){ printf '%s\n' "$1" | grep -c .; }
if ! type grepf >/dev/null 2>&1 || ! type count_first >/dev/null 2>&1 \
   || [ "$(lines "$AB")" != 3 ] || [ "$(lines "$WIFI")" != 4 ] || [ "$(lines "$PINC")" != 2 ]; then
  echo "  FAIL  could not take the checks out of $A (did their text change?)"
  echo; echo "  0 passed, 1 failed"; exit 1
fi

expect(){   # expect WANT "what this case is" "check text"
  RESULT=""; eval "$3"
  if [ "$RESULT" = "$1" ]; then passed=$((passed+1)); echo "  PASS  $2 -> $RESULT"
  else failed=$((failed+1)); echo "  FAIL  $2 -> got ${RESULT:-nothing}, want $1"; fi
}
mut(){      # mut FILE OLD NEW - replace the first OLD; a missing anchor is a failure, never a skip
  python3 - "$CAT/$1" "$2" "$3" <<'PY'
import sys
p, old, new = sys.argv[1:]
t = open(p).read()
if old not in t:
    sys.exit("anchor not found")
open(p, "w").write(t.replace(old, new, 1))
PY
}
broken(){   # broken "what this case is" - a mutation could not be applied
  failed=$((failed+1)); echo "  FAIL  $1 -> the file changed; update this test's mutation"
}

echo; echo "A/B health check (fleet-brain)"
echo "=============================="
cp pi/scripts/bridge-ab "$CAT/bridge-ab"
expect PASS "today's bridge-ab (fleet-brain only in a comment)" "$AB"
git show 3b4c345:pi/scripts/bridge-ab > "$CAT/bridge-ab"
expect FAIL "the old bridge-ab (Everything-good 3b4c345) that waited for fleet-brain" "$AB"
cp pi/scripts/bridge-ab "$CAT/bridge-ab"
if mut bridge-ab '[ -f /run/bridge-agent/last-ok ] || return 1' '# [ -f /run/bridge-agent/last-ok ] || return 1'; then
  expect FAIL "the heartbeat test survives only as a comment" "$AB"
else broken "the heartbeat test survives only as a comment"; fi

echo; echo "Setup-AP password (the fixed bridge2626 policy)"
echo "==============================================="
fresh_wifi(){ cp pi/scripts/bridge-derive-pass pi/scripts/bridge-wifi-portal.sh "$CAT/"; }
fresh_wifi; expect PASS "today's helper and portal" "$WIFI"
fresh_wifi
if mut bridge-derive-pass 'FIXED=bridge2626' 'FIXED=changeme'; then
  expect WARN "the helper enforces a different value" "$WIFI"; else broken "the helper enforces a different value"; fi
fresh_wifi
if mut bridge-wifi-portal.sh '    /usr/local/bin/bridge-derive-pass' '    echo random'; then
  expect WARN "the portal no longer runs the helper (the -x test line is still there)" "$WIFI"
else broken "the portal no longer runs the helper"; fi
fresh_wifi
if mut bridge-derive-pass 'FIXED=bridge2626' '# FIXED=bridge2626
FIXED=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d " ")'; then
  expect WARN "bridge2626 left only in a comment" "$WIFI"; else broken "bridge2626 left only in a comment"; fi

echo; echo "PIN: a wrong try is counted before the PIN is checked"
echo "====================================================="
cp pi/scripts/bridge-pin "$CAT/bridge-pin"
expect PASS "today's bridge-pin" "$PINC"
git show 3b4c345:pi/scripts/bridge-pin > "$CAT/bridge-pin"
expect FAIL "the old bridge-pin (Everything-good 3b4c345)" "$PINC"
cp pi/scripts/bridge-pin "$CAT/bridge-pin"
if mut bridge-pin '            write_atomic(TRIES, "%d\n" % n, 0o600)' '            pass' \
   && mut bridge-pin '        if not verify(pin, stored):' '        if not verify(pin, stored):
            write_atomic(TRIES, "%d\n" % n, 0o600)'; then
  expect FAIL "the try is counted only AFTER a wrong PIN" "$PINC"; else broken "the try is counted only AFTER a wrong PIN"; fi
cp pi/scripts/bridge-pin "$CAT/bridge-pin"
if mut bridge-pin '            out({"ok": False, "reason": "error",' '            _unused = ({"ok": False, "reason": "error",'; then
  expect FAIL "a failed write no longer refuses the PIN" "$PINC"; else broken "a failed write no longer refuses the PIN"; fi
cp pi/scripts/bridge-pin "$CAT/bridge-pin"
if mut bridge-pin '            write_atomic(TRIES, "%d\n" % n, 0o600)' '            # write_atomic(TRIES, "%d\n" % n, 0o600)
            pass'; then
  expect FAIL "the count survives only as a comment" "$PINC"; else broken "the count survives only as a comment"; fi

echo; echo "The comment-blind grep itself"
echo "============================="
{ echo 'test -f /run/bridge-agent/last-ok'; yes 'filler line to overflow the pipe buffer ..........' | head -5000; } > "$CAT/big"
RESULT=""; grepf big 'bridge-agent/last-ok' && RESULT=PASS || RESULT=FAIL
[ "$RESULT" = PASS ] && { passed=$((passed+1)); echo "  PASS  a match on line 1 of a 255 KB file is found under pipefail"; } \
  || { failed=$((failed+1)); echo "  FAIL  a real match was lost (SIGPIPE under pipefail)"; }
printf '# fleet-brain\n  # fleet-brain\n' > "$CAT/comments"
grepf comments 'fleet-brain' && { failed=$((failed+1)); echo "  FAIL  a pattern inside comments counted as code"; } \
  || { passed=$((passed+1)); echo "  PASS  a pattern that appears only in comments is not found"; }

echo; echo "  $passed passed, $failed failed"
[ "$failed" -eq 0 ]
