#!/bin/bash
# Publish a built NetBridge OS version to the fleet, so bridges can install it REMOTELY:
#
#     bash tools/publish-ota.sh 2.1.0-abc1234            # then: tools/nb update <bridge> 2.1.0-abc1234
#     bash tools/publish-ota.sh 2.1.0-abc1234 --via-mac  # upload from this Mac instead
#     bash tools/publish-ota.sh --list                    # what the fleet already offers
#
# What it does:
#   1. finds the GitHub release v<version> (drafts included) and its OTA files:
#      manifest.txt, manifest.txt.sig, rootfs.tar.zst
#   2. verifies the manifest signature HERE with ~/.netbridge/keys/ota-pubkey.pem (the same key
#      every bridge pins on its read-only root) — a wrong or unsigned build never reaches the fleet
#   3. puts the three files in the fleet container at /data/payloads/ota/<version>/:
#      by default the fleet server downloads them from GitHub itself (datacenter speed; the
#      1.1 GB never crosses this Mac's uplink); --via-mac copies them from here instead
#   4. checks https://<fleet>/payloads/ota/<version>/ serves them, and that the hash matches
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
die(){ echo "❌ $*" >&2; exit 1; }
ssh_fleet(){ ssh -i "$SSHKEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }

if [ "${1:-}" = "--list" ]; then
  curl -fsS -m 20 -H "Authorization: Bearer $(tr -d '\n' < "$TOKEN_FILE")" "$FLEET/admin/payloads/ota" | python3 -m json.tool
  exit 0
fi
V="${1:?usage: publish-ota.sh <version> [--via-mac] | --list}"
VIA="${2:-}"
[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{7,40}$ ]] || die "bad version '$V' (expected like 2.1.0-abc1234)"
[ -f "$PUB" ] || die "no OTA public key at $PUB"
[ -f "$SSHKEY" ] || die "no fleet SSH key at $SSHKEY"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

echo "▶ finding release v$V on GitHub"
gh api "repos/$REPO_SLUG/releases?per_page=50" \
  --jq ".[] | select(.tag_name==\"v$V\") | .assets[] | select(.name==\"manifest.txt\" or .name==\"manifest.txt.sig\" or .name==\"rootfs.tar.zst\") | \"\(.name) \(.id) \(.size)\"" \
  > "$W/assets"
[ "$(wc -l < "$W/assets" | tr -d ' ')" = 3 ] || die "release v$V does not have manifest.txt + .sig + rootfs.tar.zst"
id_of(){ awk -v n="$1" '$1==n {print $2}' "$W/assets"; }
dl(){ gh api -H "Accept: application/octet-stream" "repos/$REPO_SLUG/releases/assets/$(id_of "$1")" > "$W/$1"; }

echo "▶ verifying the signed manifest on this Mac"
dl manifest.txt; dl manifest.txt.sig
openssl dgst -sha256 -verify "$PUB" -signature "$W/manifest.txt.sig" "$W/manifest.txt" >/dev/null \
  || die "manifest signature does NOT verify with your OTA key — not publishing"
[ "$(sed -n 's/^version=//p' "$W/manifest.txt")" = "$V" ] || die "manifest is for a different version"
[ "$(sed -n 's/^image=//p' "$W/manifest.txt")" = "rootfs.tar.zst" ] || die "manifest names an unexpected image"
WANT="$(sed -n 's/^sha256=//p' "$W/manifest.txt")"
echo "  ✅ signature OK — rootfs sha256 ${WANT:0:16}…, $(awk '$1=="rootfs.tar.zst" {printf "%.2f GB", $3/1e9}' "$W/assets")"

echo "▶ free space on the fleet server"
ssh_fleet "df -Pm / | awk 'NR==2 {print \$4\" MB free\"}'"

DEST="/data/payloads/ota/$V"
if [ "$VIA" = "--via-mac" ]; then
  echo "▶ downloading rootfs.tar.zst here, then copying to the fleet (slow on home uplinks)"
  dl rootfs.tar.zst
  [ "$(shasum -a 256 "$W/rootfs.tar.zst" | cut -d' ' -f1)" = "$WANT" ] || die "downloaded image hash mismatch"
  ssh_fleet "rm -rf /tmp/ota-$V && mkdir -p /tmp/ota-$V"
  scp -i "$SSHKEY" -o BatchMode=yes "$W/manifest.txt" "$W/manifest.txt.sig" "$W/rootfs.tar.zst" "$HOST:/tmp/ota-$V/"
  ssh_fleet "chmod 0755 /tmp/ota-$V && chmod 0644 /tmp/ota-$V/*"
else
  echo "▶ the fleet server downloads the image from GitHub directly"
  GHT="$(gh auth token)"
  # The token reaches the server over SSH on stdin, lands in a 0600 file for the download only,
  # and is deleted afterwards; it is never on a command line (ps) or in the container.
  { printf 'umask 077; set -e; rm -rf /tmp/ota-%s; mkdir -p /tmp/ota-%s; cd /tmp/ota-%s\n' "$V" "$V" "$V"
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
ssh_fleet "sudo docker compose -f $COMPOSE exec -T fleet mkdir -p $DEST && \
           sudo docker compose -f $COMPOSE cp /tmp/ota-$V/. fleet:$DEST/ && rm -rf /tmp/ota-$V"

echo "▶ checking the fleet serves it"
curl -fsS -m 20 "$FLEET/payloads/ota/$V/manifest.txt" | cmp -s - "$W/manifest.txt" || die "fleet does not serve the manifest"
LEN="$(curl -fsSI -m 20 "$FLEET/payloads/ota/$V/rootfs.tar.zst" | awk 'tolower($1)=="content-length:" {print $2}' | tr -d '\r')"
[ "$LEN" = "$(awk '$1=="rootfs.tar.zst" {print $3}' "$W/assets")" ] || die "fleet serves a rootfs of the wrong size ($LEN)"
echo "  ✅ $FLEET/payloads/ota/$V/ is ready"
echo
echo "  Install it on one bridge:   tools/nb update <bridge> $V"
echo "  Watch it:                   tools/nb ota <bridge>"
echo "  The bridge refuses while a meeting is live or the laptop is attached, writes its standby"
echo "  slot, trial-boots it, and keeps it only if it comes up healthy AND reaches the fleet."
