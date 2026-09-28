#!/usr/bin/env bash
# Ask a bridge what it is ACTUALLY running, and compare it against this repository.
#
# WHY THIS EXISTS
# ---------------
# On 26 August 2026 the audit found the P0 security fix -- the one that had blocked release --
# present in the repository, covered by 28 passing tests, recorded as SOFTWARE VERIFIED in the
# ledger, and NOT RUNNING ON THE BRIDGE. A live meeting had just gone through that bridge with
# three unauthenticated mutation endpoints exposed to the LAN.
#
# Nothing was lying. Every document said SOFTWARE VERIFIED and every document was correct.
# The gap was that "verified" had only ever been tracked against the repository, and nobody had
# asked the device. The device had the answer the whole time: it reports its own build SHA in
# /api/status, and one comparison would have surfaced it.
#
# So this is that comparison, as a first-class tool. Run it before believing any fix is live.
#
#   bash tools/fleet-drift-check.sh                 # default host
#   bash tools/fleet-drift-check.sh 192.168.1.11
#   bash tools/fleet-drift-check.sh --json          # machine-readable, for CI
#
# Exit codes:
#   0  device is running HEAD, or is behind only on commits that touch nothing it runs
#   1  device is behind on bridge-side code
#   2  device is behind on something SECURITY-RELEVANT      <-- the S-1 case
#   3  could not determine (unreachable, unknown SHA) -- never silently "fine"
set -uo pipefail
cd "$(dirname "$0")/.."

HOST="${NB_BRIDGE_HOST:-192.168.1.11}"
JSON=0
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    -*) ;;
    *) HOST="$a" ;;
  esac
done

say() { [ "$JSON" = "1" ] || printf '%b\n' "$1"; }

status=$(curl -s -m 10 "http://$HOST:8080/api/status" 2>/dev/null)
if [ -z "$status" ]; then
  say "  \033[31mUNKNOWN\033[0m  bridge at $HOST did not answer."
  say "          A bridge that cannot be asked is NOT assumed to be up to date."
  [ "$JSON" = "1" ] && echo '{"result":"unknown","reason":"unreachable","host":"'"$HOST"'"}'
  exit 3
fi

ver=$(printf '%s' "$status" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
dev=$(printf '%s' "$status" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("device_id",""))' 2>/dev/null)

# The version string is "<hand-typed marketing number>-<short git sha>". Only the second half
# carries information: the first half is a manual CI input that defaults to 2.0.0, which is how
# a July image ended up labelled 2.0.1 while August images say 2.0.0. Parse for the SHA and
# ignore the rest.
# Prefer the FULL sha from the structured release record; fall back to the short suffix of the
# version string for images built before 2026-08-26, which have no release.json. A 7-character
# prefix is not an identity -- it is a convenience that happens to be unique today.
sha=$(printf '%s' "$status" | python3 -c 'import sys,json
d=json.load(sys.stdin); b=d.get("build") or {}
print(b.get("git_sha") or "")' 2>/dev/null)
case "$sha" in ""|unknown) sha="${ver##*-}" ;; esac
if ! git cat-file -e "${sha}^{commit}" 2>/dev/null; then
  say "  \033[31mUNKNOWN\033[0m  device reports version '$ver'; '$sha' is not a commit in this repo."
  say "          Either the image was built from another tree, or the marker is malformed."
  [ "$JSON" = "1" ] && echo '{"result":"unknown","reason":"sha_not_in_repo","version":"'"$ver"'"}'
  exit 3
fi

head=$(git rev-parse --short HEAD)
say ""
say "  device        $dev  @  $HOST"
say "  running       $ver   (commit $sha)"
say "  repository    HEAD $head on $(git rev-parse --abbrev-ref HEAD)"
say ""

# Only bridge-side paths matter. A device is not "behind" because the Mac app changed.
# NOTE: no mapfile here. macOS ships bash 3.2, where mapfile does not exist; the first version
# of this script used it and died with "command not found" followed by "unbound variable".
# It failed loudly rather than reporting "in sync", which is the correct direction to fail --
# but a drift checker that cannot run on the operator's own machine is useless, so: portable.
# A newer/divergent or dirty deployment is not proven to match this checkout.
if ! git merge-base --is-ancestor "${sha}" HEAD; then
  say "  UNKNOWN: deployed commit is ahead of or diverged from this checkout."
  [ "$JSON" = "1" ] && echo '{"result":"unknown","reason":"divergent_history"}'
  exit 3
fi

missing=()
while IFS= read -r _l; do
  [ -n "$_l" ] && missing+=("$_l")
done <<EOF
$(git log --format='%h %s' "${sha}..HEAD" -- pi/ 2>/dev/null)
EOF

if [ "${#missing[@]}" -eq 0 ]; then
  say "  \033[32mIN SYNC\033[0m  the device is running every bridge-side commit on HEAD."
  [ "$JSON" = "1" ] && echo '{"result":"in_sync","version":"'"$ver"'","sha":"'"$sha"'"}'
  exit 0
fi

# Security relevance is judged on the DIFF, not on the commit subject: a subject line is a
# summary written by a human in a hurry, and the diff is what actually shipped.
#
# TWO REFINEMENTS, both learned the hard way in the first run of this script:
#
#  1. COMMENTS DO NOT COUNT. The first version flagged the golden-baseline commit as
#     security-relevant because a comment contained the word "authority". A checker that cries
#     wolf gets ignored, which is the same failure as a permanently-red CI -- so added/removed
#     lines that are pure comments are stripped before matching.
#  2. NO BARE "auth". It matches "author", "authority", "authored-by". Only the specific tokens
#     that mean authentication in this codebase are listed.
secpat='_mesh_or_local|do_POST|client_address|status=403|authoriz|authentic|authkey|api_key|bearer|token|secret|credential|shlex|shell=True|set-peer|/api/unlock|PIN|pin_set'
sec=()
for line in "${missing[@]}"; do
  c="${line%% *}"
  # keep only +/- lines, drop the ones that are entirely a comment, then match
  if git show "$c" -- pi/ 2>/dev/null \
       | grep -E '^[+-]' \
       | grep -vE '^(\+\+\+|---)' \
       | grep -vE '^[+-][[:space:]]*#' \
       | grep -qE "($secpat)"; then
    sec+=("$line")
  fi
done

say "  \033[33mBEHIND\033[0m  ${#missing[@]} bridge-side commit(s) are on HEAD but not on the device:"
for line in "${missing[@]}"; do say "      $line"; done

if [ "${#sec[@]}" -gt 0 ]; then
  say ""
  say "  \033[31m*** ${#sec[@]} OF THESE CHANGE SECURITY-RELEVANT CODE ***\033[0m"
  for line in "${sec[@]}"; do say "      $line"; done
  say ""
  say "  The fix exists in git and is NOT protecting this device. Do not describe it as"
  say "  deployed, and do not put this bridge on a network you do not control until a new"
  say "  image built from HEAD is flashed."
  if [ "$JSON" = "1" ]; then
    printf '{"result":"behind_security","version":"%s","sha":"%s","missing":%d,"security":%d}\n' \
           "$ver" "$sha" "${#missing[@]}" "${#sec[@]}"
  fi
  exit 2
fi

say ""
say "  None of the missing commits touch security-relevant code, but the device is still behind."
[ "$JSON" = "1" ] && printf '{"result":"behind","version":"%s","sha":"%s","missing":%d,"security":0}\n' \
                            "$ver" "$sha" "${#missing[@]}"
exit 1
