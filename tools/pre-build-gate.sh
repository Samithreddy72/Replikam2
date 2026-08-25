#!/usr/bin/env bash
# The gate the next image build must pass BEFORE it starts.
#
# WHY THIS EXISTS
# ---------------
# This project's expensive defects were never exotic. They were a build started from a state
# nobody had checked: a dirty tree, a stale remote, a tracked database, a repository copy
# shipped inside the image, a version typed into a text box, a release tag pointing at a
# different commit. Each was cheap to detect and costly to discover afterwards -- one of them
# only surfaced because a build was cancelled mid-flight and re-examined.
#
# An image build takes ~28 minutes and produces an artifact people flash into other
# organisations' meeting rooms. Spending 30 seconds first is not optional.
#
#   bash tools/pre-build-gate.sh              # check everything, change nothing
#   bash tools/pre-build-gate.sh --quiet      # exit code only
#
# Exit 0 = every gate passed and a build may proceed.
# Exit 1 = at least one gate failed. DO NOT BUILD.
# Exit 2 = a gate could not be evaluated. That is also DO NOT BUILD: an unevaluated gate is
#          not a passed gate, and treating it as one is the exact mistake this file guards.
set -uo pipefail
cd "$(dirname "$0")/.."

QUIET=0; [ "${1:-}" = "--quiet" ] && QUIET=1
PASS=0; FAIL=0; UNKNOWN=0

say() { [ "$QUIET" = "1" ] || printf '%b\n' "$1"; }
ok()   { PASS=$((PASS+1)); say "  \033[32mPASS\033[0m  $1"; }
no()   { FAIL=$((FAIL+1)); say "  \033[31mFAIL\033[0m  $1"; }
unk()  { UNKNOWN=$((UNKNOWN+1)); say "  \033[33mCANNOT VERIFY\033[0m  $1"; }

say ""
say "NetBridge — pre-build gate"
say "=========================="
say ""

# ---------------------------------------------------------------- source state
say "  ---- the tree you are building from ----"
dirty=$(git status --porcelain | wc -l | tr -d ' ')
[ "$dirty" = "0" ] && ok "working tree is clean" \
                   || no "working tree has $dirty uncommitted change(s) — the image would match no commit"

git fetch -q origin 2>/dev/null || true
ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")
behind=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
if [ "$ahead" = "?" ]; then
  unk "cannot compare against origin/main"
elif [ "$ahead" = "0" ] && [ "$behind" = "0" ]; then
  ok "HEAD matches origin/main ($(git rev-parse --short HEAD))"
else
  no "HEAD differs from origin/main ($ahead ahead, $behind behind) — CI would build something else"
fi

# ---------------------------------------------------------------- version source
say ""
say "  ---- version identity ----"
if [ -f VERSION ] && [ -s VERSION ]; then
  ok "./VERSION exists and is non-empty ($(tr -d ' \n\r' < VERSION))"
else
  no "./VERSION missing — the workflow would fall back to a hand-typed value"
fi
if grep -q 'tr -d .* < VERSION' .github/workflows/build-image.yml 2>/dev/null; then
  ok "the workflow reads the version from ./VERSION"
else
  no "the workflow does not read ./VERSION"
fi
if grep -q 'gh release create .* --target' .github/workflows/build-image.yml 2>/dev/null; then
  ok "the release tag is bound to the built commit (--target)"
else
  no "release would tag the branch head, not the built commit"
fi

# ---------------------------------------------------------------- what goes in the image
say ""
say "  ---- what the image will contain ----"
if grep -q 'rm -rf "\$REPO"' factory/ci-build-image.sh 2>/dev/null; then
  ok "the build-time repository copy is stripped (no .git, no source, in the image)"
else
  no "the repository would ship inside the image (~50 MB of git history)"
fi
if grep -q 'chmod 0600 /etc/default/bridge-agent' factory/ci-build-image.sh 2>/dev/null; then
  ok "the fleet bootstrap token is written 0600"
else
  no "the bootstrap token would be world-readable"
fi
if grep -q 'BOOTSTRAP_TOKEN=\.' factory/ci-build-image.sh 2>/dev/null; then
  ok "the secret sweep polices the bootstrap token's mode"
else
  no "the secret sweep is silent about the one secret deliberately baked in"
fi
if grep -q 'release.json' factory/ci-build-image.sh 2>/dev/null; then
  ok "the image records structured provenance (git sha, build id, built_at)"
else
  no "the image would carry no provenance beyond a version string"
fi

# ---------------------------------------------------------------- repo hygiene
say ""
say "  ---- repository hygiene ----"
db=$(git ls-files | grep -cE '\.(db|sqlite3?)$' || true)
[ "$db" = "0" ] && ok "no database files tracked in git" \
                || no "$db database file(s) tracked — a normal run would commit real data"

# Match VALUES, not detector definitions. The first version of this gate flagged
# factory/ci-build-image.sh, whose "leak" was its own secret-sweep pattern -- a file whose job
# is to find secrets necessarily contains strings that look like secrets. A permanent false
# alarm in a build gate gets the gate switched off, so lines that are themselves grep patterns
# are excluded. Same reasoning as stripping comments before searching source.
leak=$(git grep -nIE 'tskey-auth-[A-Za-z0-9]{10}|AKIA[0-9A-Z]{16}|BEGIN (RSA|EC|OPENSSH) PRIVATE' \
        -- . 2>/dev/null \
        | grep -vE '^(tests|docs)/' \
        | grep -vE 'grep -|grep_|SECRET_LINE|LEAKS=|secpat=|pattern' \
        | wc -l | tr -d ' ')
if [ "$leak" = "0" ]; then
  ok "no credential-shaped VALUES in tracked source (detector patterns excluded)"
else
  no "$leak tracked line(s) contain credential-shaped values"
  say "        $(git grep -nIE 'tskey-auth-[A-Za-z0-9]{10}|AKIA[0-9A-Z]{16}|BEGIN (RSA|EC|OPENSSH) PRIVATE' -- . 2>/dev/null | grep -vE '^(tests|docs)/' | grep -vE 'grep -|grep_|SECRET_LINE|LEAKS=|secpat=|pattern' | head -3 | cut -c1-100)"
fi

# ---------------------------------------------------------------- tests
say ""
say "  ---- verification ----"
PY=/tmp/bev/bin/python; [ -x "$PY" ] || PY=python3
out=$(bash tools/run-tests.sh --python "$PY" 2>/dev/null | grep -oE 'TOTAL [0-9]+ passed, [0-9]+ failed, [0-9]+ skipped, [0-9]+ with NO RESULT')
if [ -z "$out" ]; then
  unk "the test suite produced no summary — treat as NOT passing"
else
  f=$(printf '%s' "$out" | grep -oE '[0-9]+ failed' | grep -oE '^[0-9]+')
  n=$(printf '%s' "$out" | grep -oE '[0-9]+ with NO RESULT' | grep -oE '^[0-9]+')
  if [ "$f" = "0" ] && [ "$n" = "0" ]; then ok "$out"
  else no "$out"; fi
fi

# ---------------------------------------------------------------- deployment truth
say ""
say "  ---- what is deployed right now (informational, not a build blocker) ----"
if bash tools/fleet-drift-check.sh >/dev/null 2>&1; then
  ok "the bridge is running every bridge-side commit on HEAD"
else
  rc=$?
  case $rc in
    2) say "  \033[33mNOTE\033[0m  the bridge is behind on SECURITY-RELEVANT commits — which is
        precisely what this build is meant to fix. Not a blocker for BUILDING." ;;
    3) say "  \033[33mNOTE\033[0m  the bridge could not be reached; deployment state unknown." ;;
    *) say "  \033[33mNOTE\033[0m  the bridge is behind HEAD (exit $rc)." ;;
  esac
fi

# ---------------------------------------------------------------- known-good preserved
say ""
say "  ---- the fallback must survive this build ----"
kg=$(gh release view v2.0.0-1db20ec --repo Samithreddy72/Replikam2 --json assets \
      -q '.assets | length' </dev/null 2>/dev/null || echo "")
if [ -z "$kg" ]; then
  unk "could not confirm the known-good release still exists"
elif [ "$kg" -ge 5 ]; then
  ok "known-good image v2.0.0-1db20ec still published with $kg assets"
else
  no "known-good image has only $kg asset(s) — the fallback may have been damaged"
fi

say ""
say "  ────────────────────────────────────────────────"
say "  $PASS passed · $FAIL failed · $UNKNOWN cannot-verify"
if [ "$FAIL" -gt 0 ] || [ "$UNKNOWN" -gt 0 ]; then
  say "  \033[31m→ DO NOT BUILD\033[0m  (a gate that could not be evaluated is not a gate that passed)"
  [ "$FAIL" -gt 0 ] && exit 1 || exit 2
fi
say "  \033[32m→ CLEAR TO BUILD\033[0m"
exit 0
