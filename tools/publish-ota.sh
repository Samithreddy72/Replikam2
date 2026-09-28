#!/bin/bash
# Publish a built NetBridge OS version to the fleet, so bridges can install it REMOTELY:
#
#     bash tools/publish-ota.sh 2.1.0-abc1234            # then: tools/nb update <bridge> 2.1.0-abc1234
#     bash tools/publish-ota.sh 2.1.0-abc1234 --via-mac  # upload from this Mac instead
#     bash tools/publish-ota.sh 2.1.0-abc1234 --prune 3  # publish, then keep only the newest 3
#     bash tools/publish-ota.sh --prune 3                 # free space: keep only the newest 3
#     bash tools/publish-ota.sh --list                    # what the fleet already offers
#
# What it does:
#   1. finds the GitHub release v<version> (drafts included) and its OTA files:
#      manifest.txt, manifest.txt.sig, rootfs.tar.zst
#   2. verifies the manifest signature HERE with ~/.netbridge/keys/ota-pubkey.pem (the same key
#      every bridge pins on its read-only root) — a wrong or unsigned build never reaches the fleet
#   3. refuses unless the fleet server has room for it: twice the image (it sits in /tmp on the
#      host while it is copied into the container) plus FLEET_MIN_FREE_MB (default 1024) left
#      over, because the same disk holds bridge.db and a full disk stops telemetry and commands
#   4. puts the three files in the fleet container at /data/payloads/ota/.incoming-<version>/:
#      by default the fleet server downloads them from GitHub itself (datacenter speed; the
#      1.1 GB never crosses this Mac's uplink); --via-mac copies them from here instead. Only
#      when all three are there and the image is its full size is the directory renamed to
#      /data/payloads/ota/<version>/, in one step
#   5. checks https://<fleet>/payloads/ota/<version>/ serves them, and that the size matches
#   6. with --prune N: keeps the N most recently published versions the fleet offers and removes
#      the other ones; a version that was never installable (no full image, or unsigned) is
#      removed once it has been untouched for 6 hours. Never the one just published, never one
#      an active or paused rollout uses. Also clears what a killed publish left behind, in the
#      container and in /tmp on the host. Asks first, or --yes
#
# The bridge verifies everything again before writing a single byte (bridge-update.sh), and
# refuses a root filesystem that is not a complete bridge root.
set -euo pipefail
FLEET="${FLEET_URL:-https://fleet.scine.online}"
HOST="${FLEET_HOST:-ubuntu@100.29.201.7}"
SSHKEY="${FLEET_SSH_KEY:-$HOME/.ssh/netbridge-fleet.pem}"
COMPOSE="${FLEET_COMPOSE:-/opt/netbridge/docker-compose.yml}"
REPO_SLUG="${REPO_SLUG:-Samithreddy72/Replikam2}"
PUB="$HOME/.netbridge/keys/ota-pubkey.pem"
TOKEN_FILE="${FLEET_TOKEN_FILE:-$HOME/.netbridge/fleet-automation-token}"
OTA="${FLEET_OTA_DIR:-/data/payloads/ota}"      # inside the fleet container (the fleetdata volume)
HOST_TMP="${FLEET_TMP:-/tmp}"                   # on the fleet host
MIN_FREE_MB="${FLEET_MIN_FREE_MB:-1024}"
VRE='^[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{7,40}$'
die(){ echo "❌ $*" >&2; exit 1; }
ssh_fleet(){ ssh -i "$SSHKEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }
# A shell script on stdin, run INSIDE the fleet container (no nested quoting to get wrong).
in_fleet(){ ssh_fleet "sudo docker compose -f $COMPOSE exec -T fleet sh -s"; }
token(){ [ -f "$TOKEN_FILE" ] || die "no fleet token at $TOKEN_FILE"; tr -d '\n' < "$TOKEN_FILE"; }

V="" VIA="" PRUNE="" YES="" LIST=""
while [ $# -gt 0 ]; do case "$1" in
  --list) LIST=1; shift ;;
  --via-mac) VIA=1; shift ;;
  --prune) [[ "${2-}" =~ ^[1-9][0-9]*$ ]] || die "--prune needs how many versions to keep (1 or more)"
           PRUNE="$2"; shift 2 ;;
  --yes) YES=1; shift ;;
  -h|--help) awk 'NR > 1 { if (/^#/) print; else exit }' "$0"; exit 0 ;;
  -*) die "unknown option $1 (see --help)" ;;
  *) [ -z "$V" ] || die "one version at a time"; V="$1"; shift ;;
esac; done

if [ -n "$LIST" ]; then
  curl -fsS -m 20 -H "Authorization: Bearer $(token)" "$FLEET/admin/payloads/ota" | python3 -m json.tool
  exit 0
fi
[ -n "$V$PRUNE" ] || die "usage: publish-ota.sh <version> [--via-mac] [--prune N [--yes]] | --prune N [--yes] | --list"
[ -z "$V" ] || [[ "$V" =~ $VRE ]] || die "bad version '$V' (expected like 2.1.0-abc1234)"
[[ "$MIN_FREE_MB" =~ ^[0-9]+$ ]] || die "FLEET_MIN_FREE_MB must be a whole number of MB, not '$MIN_FREE_MB'"
[ -f "$SSHKEY" ] || die "no fleet SSH key at $SSHKEY"

# The versions the fleet OFFERS: complete and signed. The same list the panel and `nb update` use.
# Installable = signed AND its image is there. Checked here, not left to the server: a fleet still
# running the server from before 2026-09-28 lists an image-less leftover with a .sig as "signed"
# with "bytes": null, and --prune would keep that leftover as one of the newest N and remove the
# last real image (review, 2026-09-28).
offered_versions(){
  curl -fsS -m 20 -H "Authorization: Bearer $(token)" "$FLEET/admin/payloads/ota" \
    | python3 -c 'import json,sys; [print(x["version"]) for x in json.load(sys.stdin) if x.get("signed") and x.get("bytes")]'
}
# The versions an unfinished (active or paused) rollout still sends bridges to: its version, and
# the /payloads/ota/<v> its update commands actually fetch, in case the two differ. The fleet is
# single-org (config.py), so the admin's view is every rollout; the day it hosts several orgs,
# this has to look across all of them.
busy_versions(){
  curl -fsS -m 20 -H "Authorization: Bearer $(token)" "$FLEET/admin/rollouts" \
    | python3 -c '
import json, re, sys
for r in json.load(sys.stdin):
    if r.get("status") in ("active", "paused"):
        print(r.get("version") or "")
        m = re.search(r"/payloads/ota/([^/?#]+)", r.get("source") or "")
        if m:
            print(m.group(1))'
}

prune(){ # prune <keep> [version just published]
  local keep="$1" fresh="${2:-}" listing all stale offered busy v why kept=0 del=() whys=() left=() i a
  echo "▶ removing old OS versions from the fleet (keeping the newest $keep)"
  # What is on the disk, newest first by publish time (a version's directory is created when it
  # is published), and which directories have not changed for 6 hours. Read BEFORE asking the
  # fleet what it offers, so a version renamed into place in between is simply not seen here,
  # never mistaken for an incomplete one.
  listing="$(printf '%s\n' "cd '$OTA' 2>/dev/null || exit 0" 'ls -1t' 'echo --' \
               "find . -mindepth 1 -maxdepth 1 -type d -mmin +360 | sed 's|^\\./||'" | in_fleet)" \
    || die "could not list the versions on the fleet"
  all="$(awk '/^--$/ {exit} {print}' <<<"$listing")"
  stale="$(awk 'f; /^--$/ {f=1}' <<<"$listing")"
  # A version an unfinished rollout points at stays: its remaining waves would fetch a 404 and
  # stall. If the fleet cannot say which those are, or what it offers, nothing is removed.
  offered="$(offered_versions)" || die "could not read which versions the fleet offers — nothing removed"
  busy="$(busy_versions)" || die "could not read the fleet's rollouts — nothing removed"
  while IFS= read -r v; do
    [[ "$v" =~ $VRE ]] || continue
    if grep -qxF -- "$v" <<<"$offered"; then
      # Only a version a bridge can install counts towards the N kept (2026-09-28): the image-less
      # directory an interrupted old-style publish left behind, newer than the good versions,
      # would otherwise take a place and push the last good version out.
      if [ "$kept" -lt "$keep" ]; then kept=$((kept+1)); echo "  keeping $v (one of the newest $keep)"; continue; fi
      [ "$v" != "$fresh" ] || continue
      why=""
    else
      why="  (never installable: no full image, or unsigned)"
    fi
    if grep -qxF -- "$v" <<<"$busy"; then echo "  keeping $v (an unfinished rollout uses it)"; continue; fi
    if [ -n "$why" ] && ! grep -qxF -- "$v" <<<"$stale"; then
      echo "  leaving $v alone: it is not installable, but it changed in the last 6 hours"; continue
    fi
    del+=("$v"); whys+=("$why")
  done <<<"$all"
  if [ ${#del[@]} -eq 0 ]; then
    echo "  nothing to remove"
  else
    echo "  will remove:"
    for i in "${!del[@]}"; do echo "    ${del[$i]}${whys[$i]}"; done
    if [ -z "$YES" ]; then
      [ -t 0 ] || die "not removing anything without confirmation — re-run with --yes"
      read -r -p "  Remove these ${#del[@]} version(s) from the fleet? [y/N] " a || a=""
      [[ "$a" =~ ^[Yy] ]] || die "nothing removed"
      # The question can sit unanswered for a while: a rollout started in the panel meanwhile
      # keeps its version.
      busy="$(busy_versions)" || die "could not read the fleet's rollouts — nothing removed"
      for v in "${del[@]}"; do
        if grep -qxF -- "$v" <<<"$busy"; then echo "  keeping $v (a rollout started using it)"; else left+=("$v"); fi
      done
      del=(${left[@]+"${left[@]}"})
    fi
  fi
  # Each version is renamed out of the catalog before it is deleted, so the panel never lists
  # one whose image is half gone. Staging left by a publish that was killed mid-copy (untouched
  # for 6 hours, so never a publish still running) goes too.
  { echo "set -e; cd '$OTA' 2>/dev/null || exit 0"
    for v in ${del[@]+"${del[@]}"}; do
      echo "if [ -e '$v' ]; then rm -rf '.old-$v'; mv '$v' '.old-$v'; rm -rf '.old-$v'; fi"
    done
    echo "find . -mindepth 1 -maxdepth 1 \\( -name '.incoming-*' -o -name '.old-*' \\) -mmin +360 -exec rm -rf {} +"
  } | in_fleet || die "removing old versions failed"
  # ... and what such a publish left in /tmp on the host, where the image lands before it is
  # copied in. Without this, "not enough space - run --prune" could not free that 1.1 GB.
  ssh_fleet "find '$HOST_TMP' -mindepth 1 -maxdepth 1 -type d -name 'ota-[0-9]*' -mmin +360 -exec rm -rf {} +" \
    || echo "  ⚠ could not clear old downloads from $HOST_TMP on the fleet server"
  [ ${#del[@]} -eq 0 ] || echo "  ✅ removed ${#del[@]} version(s)"
  ssh_fleet "df -Pm / | awk 'NR==2 {print \"  \" \$4 \" MB free on the fleet server\"}'" || true
}

if [ -z "$V" ]; then prune "$PRUNE"; exit 0; fi

[ -f "$PUB" ] || die "no OTA public key at $PUB"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
HTMP="$HOST_TMP/ota-$V"                 # on the host, while it is downloaded / copied in
STAGE="$OTA/.incoming-$V"               # in the container, while it is copied in
DEST="$OTA/$V"                          # what bridges fetch and the catalog lists

echo "▶ finding release v$V on GitHub"
gh api "repos/$REPO_SLUG/releases?per_page=50" \
  --jq ".[] | select(.tag_name==\"v$V\") | .assets[] | select(.name==\"manifest.txt\" or .name==\"manifest.txt.sig\" or .name==\"rootfs.tar.zst\") | \"\(.name) \(.id) \(.size)\"" \
  > "$W/assets"
[ "$(wc -l < "$W/assets" | tr -d ' ')" = 3 ] || die "release v$V does not have manifest.txt + .sig + rootfs.tar.zst"
id_of(){ awk -v n="$1" '$1==n {print $2}' "$W/assets"; }
dl(){ gh api -H "Accept: application/octet-stream" "repos/$REPO_SLUG/releases/assets/$(id_of "$1")" > "$W/$1"; }
IMG_BYTES="$(awk '$1=="rootfs.tar.zst" {print $3}' "$W/assets")"
[[ "$IMG_BYTES" =~ ^[0-9]+$ ]] || die "GitHub did not say how big rootfs.tar.zst is"

echo "▶ verifying the signed manifest on this Mac"
dl manifest.txt; dl manifest.txt.sig
openssl dgst -sha256 -verify "$PUB" -signature "$W/manifest.txt.sig" "$W/manifest.txt" >/dev/null \
  || die "manifest signature does NOT verify with your OTA key — not publishing"
[ "$(sed -n 's/^version=//p' "$W/manifest.txt")" = "$V" ] || die "manifest is for a different version"
[ "$(sed -n 's/^image=//p' "$W/manifest.txt")" = "rootfs.tar.zst" ] || die "manifest names an unexpected image"
WANT="$(sed -n 's/^sha256=//p' "$W/manifest.txt")"
MSIZE="$(sed -n 's/^size=//p' "$W/manifest.txt")"
[ -z "$MSIZE" ] || [ "$MSIZE" = "$IMG_BYTES" ] || die "the manifest says the image is $MSIZE bytes, GitHub has $IMG_BYTES"
echo "  ✅ signature OK — rootfs sha256 ${WANT:0:16}…, $(awk -v b="$IMG_BYTES" 'BEGIN {printf "%.2f GB", b/1e9}')"

echo "▶ free space on the fleet server"
# This used to be only printed, which let a publish fill the disk that also holds bridge.db
# (2026-09-28).
FREE_MB="$(ssh_fleet "df -Pm / | awk 'NR==2 {print \$4}'")" || die "could not read the fleet server's free space"
[[ "$FREE_MB" =~ ^[0-9]+$ ]] || die "could not read the fleet server's free space ('$FREE_MB')"
NEED_MB=$(( 2 * ((IMG_BYTES + 1048575) / 1048576) + MIN_FREE_MB ))
echo "  $FREE_MB MB free, $NEED_MB MB needed (the image twice while it is copied in, plus $MIN_FREE_MB MB kept free)"
[ "$FREE_MB" -ge "$NEED_MB" ] \
  || die "not enough space on the fleet server — remove old versions first: bash tools/publish-ota.sh --prune 2"

if [ -n "$VIA" ]; then
  echo "▶ downloading rootfs.tar.zst here, then copying to the fleet (slow on home uplinks)"
  dl rootfs.tar.zst
  [ "$(shasum -a 256 "$W/rootfs.tar.zst" | cut -d' ' -f1)" = "$WANT" ] || die "downloaded image hash mismatch"
  ssh_fleet "rm -rf '$HTMP' && mkdir -p '$HTMP'"
  scp -i "$SSHKEY" -o BatchMode=yes "$W/manifest.txt" "$W/manifest.txt.sig" "$W/rootfs.tar.zst" "$HOST:$HTMP/" \
    || { ssh_fleet "rm -rf '$HTMP'" || true; die "copying to the fleet server failed"; }
  ssh_fleet "chmod 0755 '$HTMP' && chmod 0644 '$HTMP'/*" \
    || { ssh_fleet "rm -rf '$HTMP'" || true; die "could not set the permissions of the copy on the fleet server"; }
else
  echo "▶ the fleet server downloads the image from GitHub directly"
  GHT="$(gh auth token)"
  # The token reaches the server over SSH on stdin, lands in a 0600 file for the download only,
  # and is deleted afterwards; it is never on a command line (ps) or in the container. A failed
  # download (curl gives up, or the hash or the manifest does not match) removes its partial
  # 1.1 GB, and the token file with it, instead of leaving them on the disk; whatever a publish
  # killed from this end leaves behind, --prune clears once it is 6 hours old.
  { printf 'umask 077; set -e; trap '"'"'[ $? -eq 0 ] || rm -rf "%s"'"'"' EXIT\n' "$HTMP"
    printf 'rm -rf "%s"; mkdir -p "%s"; cd "%s"\n' "$HTMP" "$HTMP" "$HTMP"
    printf 'printf "header = \\"Authorization: Bearer %%s\\"\\n" "%s" > .auth\n' "$GHT"
    for n in manifest.txt manifest.txt.sig rootfs.tar.zst; do
      printf 'curl -fsSL -K .auth -H "Accept: application/octet-stream" -o %s https://api.github.com/repos/%s/releases/assets/%s\n' \
        "$n" "$REPO_SLUG" "$(id_of "$n")"
    done
    printf 'rm -f .auth; chmod 0755 .; chmod 0644 manifest.txt manifest.txt.sig rootfs.tar.zst\n'
    printf '[ "$(sha256sum rootfs.tar.zst | cut -d" " -f1)" = "%s" ] || { echo "hash mismatch on the server"; exit 9; }\n' "$WANT"
    printf 'cmp -s manifest.txt /dev/stdin <<"EOF_M"\n%s\nEOF_M\n' "$(cat "$W/manifest.txt")"
  } | ssh_fleet "bash -s" || die "server-side download failed (try --via-mac)"
fi

echo "▶ placing the files in the fleet container ($DEST)"
# Staged, then renamed (2026-09-28). Copied straight into ota/<version>/, the version was in the
# catalog - with "Install on..." - for the minutes the 1.1 GB copy took, and a bridge that
# fetched it then got a truncated image. A rename within one directory is atomic: bridges and
# the panel see no version, then the whole version.
unstage(){ echo "rm -rf '$STAGE'" | in_fleet || true; ssh_fleet "rm -rf '$HTMP'" || true; }
echo "rm -rf '$STAGE'; mkdir -p '$STAGE'" | in_fleet || { unstage; die "could not create $STAGE in the fleet container"; }
ssh_fleet "sudo docker compose -f $COMPOSE cp '$HTMP/.' 'fleet:$STAGE/'" \
  || { unstage; die "copying into the fleet container failed — nothing published"; }
ssh_fleet "rm -rf '$HTMP'" || true
in_fleet <<EOF || { unstage; die "the copy in the fleet container is incomplete — nothing published"; }
set -e
cd '$STAGE'
[ -s manifest.txt ] && [ -s manifest.txt.sig ] && [ -f rootfs.tar.zst ] || { echo "files missing in the container" >&2; exit 1; }
got=\$(wc -c < rootfs.tar.zst | tr -d ' ')
[ "\$got" = "$IMG_BYTES" ] || { echo "rootfs.tar.zst is \$got bytes in the container, expected $IMG_BYTES" >&2; exit 1; }
cd '$OTA'
rm -rf '.old-$V'
if [ -e '$V' ]; then mv '$V' '.old-$V'; fi
mv '.incoming-$V' '$V' || { [ ! -e '.old-$V' ] || mv '.old-$V' '$V'; exit 1; }
rm -rf '.old-$V' || true          # published already; a later --prune clears what is left
EOF

echo "▶ checking the fleet serves it"
curl -fsS -m 20 "$FLEET/payloads/ota/$V/manifest.txt" | cmp -s - "$W/manifest.txt" || die "fleet does not serve the manifest"
LEN="$(curl -fsSI -m 20 "$FLEET/payloads/ota/$V/rootfs.tar.zst" | awk 'tolower($1)=="content-length:" {print $2}' | tr -d '\r')"
[ "$LEN" = "$IMG_BYTES" ] || die "fleet serves a rootfs of the wrong size ($LEN)"
if [ -f "$TOKEN_FILE" ]; then
  o="$(offered_versions)" || die "could not ask the fleet which versions it offers"
  grep -qxF -- "$V" <<<"$o" || die "the fleet serves the files but does not offer $V as installable"
fi
echo "  ✅ $FLEET/payloads/ota/$V/ is ready"

[ -z "$PRUNE" ] || prune "$PRUNE" "$V"
echo
echo "  Install it on one bridge:   tools/nb update <bridge> $V"
echo "  Watch it:                   tools/nb ota <bridge>"
echo "  The bridge refuses while a meeting is live or the laptop is attached, writes its standby"
echo "  slot, trial-boots it, and keeps it only if it comes up healthy AND reaches the fleet."
