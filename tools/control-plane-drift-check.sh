#!/usr/bin/env bash
# Ask the LIVE control plane what it is running, and compare it against this repository.
#
# WHY THIS EXISTS
# ---------------
# The bridge had this problem first: a security fix sat in git, fully tested, while the device
# ran an older image, and nothing could tell the difference. tools/fleet-drift-check.sh closed
# that for devices.
#
# The control plane had exactly the same gap and nobody had looked. /docs, /redoc and
# /openapi.json were closed in source on 26 Aug and were still being served publicly by
# fleet.scine.online afterwards -- 35 endpoints including 23 admin routes. The only reason it
# was noticed is that someone probed the live server's behaviour. The backend reported no
# version, no commit and no build id, so "fixed in the repo" and "fixed in production" were
# indistinguishable from outside.
#
#   bash tools/control-plane-drift-check.sh                      # default host
#   bash tools/control-plane-drift-check.sh fleet.example.com
#
# Exit codes:
#   0  live server matches HEAD, and no debug surface is exposed
#   1  live server is behind HEAD
#   2  live server is behind on SECURITY-RELEVANT code, or exposes debug surface
#   3  cannot determine -- unreachable, or the server reports no identity at all
set -uo pipefail
cd "$(dirname "$0")/.."

HOST="${1:-${NB_FLEET_HOST:-fleet.scine.online}}"
BASE="https://$HOST"

say() { printf '%b\n' "$1"; }

health=$(curl -fsS -m 15 "$BASE/healthz" 2>/dev/null)
if [ -z "$health" ]; then
  say "  \033[31mUNKNOWN\033[0m  $HOST/healthz did not answer."
  say "          A control plane that cannot be asked is NOT assumed current."
  exit 3
fi

sha=$(printf '%s' "$health" | sed -n 's/.*"git_sha" *: *"\([^"]*\)".*/\1/p')
built=$(printf '%s' "$health" | sed -n 's/.*"built_at" *: *"\([^"]*\)".*/\1/p')
bid=$(printf '%s' "$health" | sed -n 's/.*"build_id" *: *"\([^"]*\)".*/\1/p')

say ""
say "  control plane   $HOST"
say "  reports         git_sha=${sha:-<none>}  build=${bid:-<none>}  built_at=${built:-<none>}"
say "  repository      HEAD $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
say ""

# --- debug surface, checked FIRST because it needs no identity to detect -------------------
exposed=""
for ep in /docs /redoc /openapi.json; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$BASE$ep" 2>/dev/null)
  [ "$code" = "200" ] && exposed="$exposed $ep"
done
if [ -n "$exposed" ]; then
  say "  \033[31mDEBUG SURFACE EXPOSED\033[0m ${exposed}"
  say "          Interactive API documentation is publicly readable. It discloses every"
  say "          endpoint, including admin routes, to anyone who asks. Set NB_API_DOCS unset"
  say "          (the default) and redeploy."
else
  say "  \033[32mOK\033[0m  no public API documentation (/docs, /redoc, /openapi.json all closed)"
fi

# --- identity ------------------------------------------------------------------------------
if [ -z "$sha" ] || [ "$sha" = "unknown" ]; then
  say ""
  say "  \033[31mUNKNOWN\033[0m  the live server reports no commit identity."
  say "          It is running a build from before the identity work, so what is deployed"
  say "          cannot be established from outside. Redeploy with deploy.sh, which stamps it."
  [ -n "$exposed" ] && exit 2
  exit 3
fi

clean="${sha%-dirty}"
[ "$clean" != "$sha" ] && say "  \033[33mNOTE\033[0m  the live build was made from a DIRTY tree; it matches no commit exactly."

if ! git cat-file -e "${clean}^{commit}" 2>/dev/null; then
  say "  \033[31mUNKNOWN\033[0m  $clean is not a commit in this repository."
  exit 3
fi

# A newer/divergent or dirty deployment is not proven to match this checkout.
if ! git merge-base --is-ancestor "${clean}" HEAD; then
  say "  UNKNOWN: deployed commit is ahead of or diverged from this checkout."
  exit 3
fi
if [ "$clean" != "$sha" ]; then
  say "  UNKNOWN: a dirty deployed build cannot be verified against a commit."
  exit 3
fi

missing=()
while IFS= read -r _l; do
  [ -n "$_l" ] && missing+=("$_l")
done <<EOF
$(git log --format='%h %s' "${clean}..HEAD" -- control-plane/ 2>/dev/null)
EOF

if [ "${#missing[@]}" -eq 0 ]; then
  say ""
  say "  \033[32mIN SYNC\033[0m  the live control plane is running every control-plane commit on HEAD."
  [ -n "$exposed" ] && exit 2
  exit 0
fi

# Same reasoning as the fleet checker: judge the DIFF, and strip comments first so the comment
# explaining a fix cannot be mistaken for the absence of one.
secpat='CONFIRM_REQUIRED|NO_DOUBLE_EXECUTE|require_admin|require_device|require_viewer|_scoped|org_id|token|secret|docs_url|openapi_url|idempotency'
sec=()
for line in "${missing[@]}"; do
  c="${line%% *}"
  if git show "$c" -- control-plane/ 2>/dev/null \
       | grep -E '^[+-]' | grep -vE '^[+-][[:space:]]*#' | grep -qE "($secpat)"; then
    sec+=("$line")
  fi
done

say ""
say "  \033[33mBEHIND\033[0m  ${#missing[@]} control-plane commit(s) are on HEAD but not deployed:"
for line in "${missing[@]}"; do say "      $line"; done

if [ "${#sec[@]}" -gt 0 ]; then
  say ""
  say "  \033[31m*** ${#sec[@]} OF THESE CHANGE SECURITY-RELEVANT CODE ***\033[0m"
  for line in "${sec[@]}"; do say "      $line"; done
  say ""
  say "  These fixes exist in git and are NOT protecting the live fleet."
  exit 2
fi
[ -n "$exposed" ] && exit 2
exit 1
