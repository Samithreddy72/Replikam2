#!/bin/bash
# Push the fleet's secrets to Fly.io from your local ~/.netbridge files.
# Run this AFTER `fly launch` has created the app. Values are piped via stdin
# (fly secrets import), never placed on the command line, so they don't leak
# into shell history or `ps`.
#
#   bash fly-secrets.sh <fly-app-name>
set -euo pipefail

APP="${1:?usage: bash fly-secrets.sh <fly-app-name>}"
N="$HOME/.netbridge"
GMAIL="samithreddy72@gmail.com"

for f in ts-oauth-secret bootstrap-token smtp-app-password; do
  [ -s "$N/$f" ] || { echo "missing $N/$f"; exit 1; }
done

# A fresh bootstrap admin key (used once to create your first admin on the new
# server, then it retires automatically).
ADMIN_KEY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"

{
  echo "TS_API_KEY=$(cat "$N/ts-oauth-secret")"
  echo "BOOTSTRAP_TOKENS=$(cat "$N/bootstrap-token")"
  echo "SMTP_PASSWORD=$(tr -d '[:space:]' < "$N/smtp-app-password")"
  echo "SMTP_USER=$GMAIL"
  echo "ALERT_EMAIL_FROM=$GMAIL"
  echo "ALERT_EMAIL_TO=$GMAIL"
  echo "ADMIN_API_KEY=$ADMIN_KEY"
} | fly secrets import -a "$APP"

echo
echo "secrets pushed to $APP."
echo "your one-time bootstrap admin key (save it somewhere for the sign-in step):"
echo "  $ADMIN_KEY"
