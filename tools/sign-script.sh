#!/bin/bash
# Sign a bridge script for remote deploy, and (optionally) push it.
#
# The private key never leaves this Mac; the bridge only ever holds the public half. Same
# EC/SHA256 primitive as the image updater — a script override is code, so it gets the same
# treatment code gets.
#
#   sign-script.sh <path/to/script.sh> [outdir]     sign into outdir (default ./signed)
#   sign-script.sh --keygen                          create the keypair (once)
#   sign-script.sh --pubkey                          print the public key to install on a bridge
set -uo pipefail
KEY="$HOME/.netbridge/keys/script-signing-key.pem"
PUB="$HOME/.netbridge/keys/script-pubkey.pem"

if [ "${1:-}" = "--keygen" ]; then
  [ -f "$KEY" ] && { echo "key already exists at $KEY — refusing to overwrite"; exit 1; }
  mkdir -p "$(dirname "$KEY")"
  openssl ecparam -name prime256v1 -genkey -noout -out "$KEY"
  chmod 600 "$KEY"
  openssl ec -in "$KEY" -pubout -out "$PUB" 2>/dev/null
  echo "created $KEY (private, chmod 600)"
  echo "created $PUB  -> install this on each bridge as /data/config/script-pubkey.pem"
  exit 0
fi
[ "${1:-}" = "--pubkey" ] && { cat "$PUB"; exit 0; }

SRC="${1:?usage: sign-script.sh <script.sh> [outdir] | --keygen | --pubkey}"
OUT="${2:-./signed}"
[ -f "$KEY" ] || { echo "no signing key — run: $0 --keygen"; exit 2; }
[ -f "$SRC" ] || { echo "no such file: $SRC"; exit 2; }
bash -n "$SRC" || { echo "refusing to sign a script that fails syntax check"; exit 3; }

mkdir -p "$OUT"
n="$(basename "$SRC")"
cp "$SRC" "$OUT/$n"
openssl dgst -sha256 -sign "$KEY" -out "$OUT/$n.sig" "$OUT/$n"
openssl dgst -sha256 -verify "$PUB" -signature "$OUT/$n.sig" "$OUT/$n" >/dev/null \
  || { echo "self-verify FAILED — do not deploy"; exit 4; }
echo "signed $OUT/$n"
echo "  sha256 $(shasum -a 256 "$OUT/$n" | cut -c1-16)…  sig $(wc -c < "$OUT/$n.sig" | tr -d ' ') bytes  self-verify OK"
