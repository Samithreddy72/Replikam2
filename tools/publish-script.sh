#!/bin/bash
# Sign a bridge script and publish it to the FLEET, so any bridge anywhere can fetch it.
#
#     bash tools/publish-script.sh pi/scripts/bridge-return-audio.sh
#
# WHY THE FLEET AND NOT A LAPTOP
# `deploy-script` works by telling the BRIDGE to download a signed script from a URL — so
# that URL has to be somewhere the bridge can reach. Serving it from this Mac failed: a
# bridge on a venue network cannot route here, the fetch hung, and the command died with no
# error anyone could see. Meanwhile every bridge already talks to fleet.scine.online over
# public HTTPS every 15 seconds, from anywhere in the world. That was the answer all along.
#
# The private signing key never leaves this machine. The fleet only ever holds the signed
# artefact, and the device verifies it twice — at install, and again at every service start —
# against a public key on its READ-ONLY root, where the override mechanism cannot reach it.
set -uo pipefail
SRC="${1:?usage: publish-script.sh <path/to/script.sh>}"
FLEET="${FLEET_URL:-https://fleet.scine.online}"
TOKEN_FILE="${FLEET_TOKEN_FILE:-$HOME/.netbridge/fleet-automation-token}"
KEY="$HOME/.netbridge/keys/script-signing-key.pem"

[ -f "$SRC" ]   || { echo "❌ no such file: $SRC" >&2; exit 1; }
[ -f "$KEY" ]   || { echo "❌ no signing key at $KEY — run: tools/sign-script.sh --keygen" >&2; exit 2; }
[ -f "$TOKEN_FILE" ] || { echo "❌ no fleet token at $TOKEN_FILE" >&2; exit 2; }
NAME="$(basename "$SRC")"
case "$NAME" in *.sh) : ;; *) echo "❌ must be a .sh" >&2; exit 1 ;; esac

bash -n "$SRC" || { echo "❌ refusing to publish a script that fails syntax check" >&2; exit 3; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$SRC" "$TMP/$NAME"
openssl dgst -sha256 -sign "$KEY" -out "$TMP/$NAME.sig" "$TMP/$NAME" || exit 4
# Self-verify before upload: publishing an unverifiable payload would just waste a
# round-trip to the device, which then refuses it anyway.
openssl dgst -sha256 -verify "${KEY%-key.pem}-pubkey.pem" -signature "$TMP/$NAME.sig" "$TMP/$NAME" >/dev/null 2>&1 \
  || openssl dgst -sha256 -verify "$HOME/.netbridge/keys/script-pubkey.pem" -signature "$TMP/$NAME.sig" "$TMP/$NAME" >/dev/null \
  || { echo "❌ self-verify failed — not publishing" >&2; exit 4; }
echo "  ✅ signed  $NAME  sha256 $(shasum -a 256 "$TMP/$NAME" | cut -c1-16)…"

R=$(curl -s -m 60 -X POST "$FLEET/admin/payloads" \
      -H "Authorization: Bearer $(tr -d '\n' < "$TOKEN_FILE")" \
      -F "name=$NAME" -F "script=@$TMP/$NAME" -F "sig=@$TMP/$NAME.sig")
echo "$R" | grep -q '"name"' || { echo "❌ upload failed: $R" >&2; exit 5; }
echo "  ✅ published to the fleet: $R"
echo
echo "  Now deploy it from the panel:  Actions → Deploy a signed script…"
echo "    script name : $NAME"
echo "    URL base    : $FLEET/payloads"
echo
echo "  The bridge fetches it over the same public HTTPS it already uses, verifies the"
echo "  signature, syntax-checks it, installs it, and restarts just that service. If the"
echo "  new script crash-loops, auto-rollback parks it and the bridge keeps working."
