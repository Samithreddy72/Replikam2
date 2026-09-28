#!/bin/bash
# Sign a NetBridge file and publish it to the FLEET, so any bridge anywhere can fetch it.
#
#     bash tools/publish-script.sh pi/scripts/bridge-web.py
#     bash tools/publish-script.sh pi/configs/owner_ssh_authorized_keys
#     bash tools/publish-script.sh my-cpu-pin.conf --name dropin.bridge-feeder-net
#
# Any file in pi/configs/updatable.conf (the same catalog the bridge enforces from its read-only
# root): scripts, Python, the owner SSH key file, systemd drop-ins. Then deploy it with
#     tools/nb deploy <bridge> <name>        (or the panel: Actions -> Deploy a signed script)
#
# WHY THE FLEET AND NOT A LAPTOP
# `deploy-script` works by telling the BRIDGE to download a signed file from a URL — so that URL
# has to be somewhere the bridge can reach. Serving it from this Mac failed: a bridge on a venue
# network cannot route here, the fetch hung, and the command died with no error anyone could
# see. Every bridge already talks to fleet.scine.online over public HTTPS every 15 seconds, from
# anywhere in the world.
#
# The private signing key never leaves this machine. The fleet only ever holds the signed
# artefact, and the device verifies it at install and again at every use, against a public key
# on its READ-ONLY root, where no update can reach it.
set -uo pipefail
SRC="" NAME=""
while [ $# -gt 0 ]; do case "$1" in
  # A --name with nothing after it used to spin forever: `shift 2` fails with one word left, so
  # the loop never moved on (2026-09-28).
  --name) [ -n "${2:-}" ] || { echo "usage: publish-script.sh <file> [--name <catalog-name>]" >&2; exit 64; }
          NAME="$2"; shift 2 ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  *) SRC="$1"; shift ;;
esac; done
[ -n "$SRC" ] || { echo "usage: publish-script.sh <file> [--name <catalog-name>]" >&2; exit 64; }
FLEET="${FLEET_URL:-https://fleet.scine.online}"
TOKEN_FILE="${FLEET_TOKEN_FILE:-$HOME/.netbridge/fleet-automation-token}"
KEY="$HOME/.netbridge/keys/script-signing-key.pem"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CATALOG="$REPO/pi/configs/updatable.conf"

[ -f "$SRC" ]   || { echo "❌ no such file: $SRC" >&2; exit 1; }
[ -f "$KEY" ]   || { echo "❌ no signing key at $KEY — run: tools/sign-script.sh --keygen" >&2; exit 2; }
[ -f "$TOKEN_FILE" ] || { echo "❌ no fleet token at $TOKEN_FILE" >&2; exit 2; }
NAME="${NAME:-$(basename "$SRC")}"
KIND="$(awk -v n="$NAME" '!/^[[:space:]]*#/ && NF>=5 && $1==n {print $3; f=1; exit} END {exit !f}' "$CATALOG")" \
  || { echo "❌ '$NAME' is not an updatable file (pi/configs/updatable.conf) — the bridge would refuse it" >&2; exit 1; }

# The same checks the bridge runs before installing — catch it here, before it is published.
case "$KIND" in
  keys)
    n=0
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|'#'*) continue ;; esac
      printf '%s\n' "$line" | ssh-keygen -l -f /dev/stdin >/dev/null 2>&1 || { echo "❌ not a valid key line: ${line:0:60}…" >&2; exit 3; }
      n=$((n+1))
    done < "$SRC"
    [ "$n" -gt 0 ] || { echo "❌ no keys in $SRC — publishing it would lock the owner out" >&2; exit 3; } ;;
  dropin)
    grep -qE '^\[(Unit|Service|Install)\]$' "$SRC" && ! grep -E '^\[' "$SRC" | grep -qvE '^\[(Unit|Service|Install)\]$' \
      || { echo "❌ a drop-in may only contain [Unit] / [Service] / [Install]" >&2; exit 3; } ;;
  *)
    first="$(head -1 "$SRC")"
    case "$first" in
      # Compile in memory, write nothing. This used py_compile with cfile=/dev/null, which raises
      # FileExistsError for ANY input (/dev/null is not a regular file) before it compiles a
      # line, so every Python file in the catalog was refused as "does not compile" and
      # `nb deploy <bridge> x.py` could never work (2026-09-28).
      '#!'*python*) python3 -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$SRC" \
                      || { echo "❌ refusing to publish Python that does not compile" >&2; exit 3; } ;;
      '#!'*bash*|'#!'*/sh*) bash -n "$SRC" || { echo "❌ refusing to publish a script that fails syntax check" >&2; exit 3; } ;;
      *) echo "❌ $NAME must start with a #!/bin/bash or python3 line" >&2; exit 3 ;;
    esac ;;
esac

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 "$REPO/pi/scripts/bridge-verify-update.py" prepare "$SRC" "$TMP/$NAME" "$NAME" || exit 4
openssl dgst -sha256 -sign "$KEY" -out "$TMP/$NAME.sig" "$TMP/$NAME" || exit 4
openssl dgst -sha256 -verify "${KEY%-key.pem}-pubkey.pem" -signature "$TMP/$NAME.sig" "$TMP/$NAME" >/dev/null 2>&1 \
  || openssl dgst -sha256 -verify "$HOME/.netbridge/keys/script-pubkey.pem" -signature "$TMP/$NAME.sig" "$TMP/$NAME" >/dev/null \
  || { echo "❌ self-verify failed — not publishing" >&2; exit 4; }
echo "  ✅ signed  $NAME ($KIND)  sha256 $(shasum -a 256 "$TMP/$NAME" | cut -c1-16)…"

R=$(curl -s -m 60 -X POST "$FLEET/admin/payloads" \
      -H "Authorization: Bearer $(tr -d '\n' < "$TOKEN_FILE")" \
      -F "name=$NAME" -F "script=@$TMP/$NAME" -F "sig=@$TMP/$NAME.sig")
echo "$R" | grep -q '"name"' || { echo "❌ upload failed: $R" >&2; exit 5; }
echo "  ✅ published to the fleet: $R"
echo
echo "  Deploy it:   tools/nb deploy <bridge> $NAME        (add --now to skip waiting for idle)"
echo "  The bridge verifies the signature, checks the file, puts it in place and applies it the"
echo "  safe way for that file (camera: only once the meeting laptop is unplugged). If it then"
echo "  fails, automatic rollback puts the built-in file back."
