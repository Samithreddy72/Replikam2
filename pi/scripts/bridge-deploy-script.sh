#!/bin/bash
# Install a signed script override into /data/overrides and restart its service.
#
# Called by the fleet agent (command: deploy-script) or by hand over SSH. It NEVER trusts the
# payload: the signature is verified here, before anything is installed, and again by
# bridge-run.sh at every service start. Two independent checks, because this is the one path
# that lets a remote party change what code the bridge executes.
#
#   bridge-deploy-script.sh <name> <url-base>     fetch <name> and <name>.sig from url-base
#   bridge-deploy-script.sh --revert <name>       drop the override, back to baked-in
#   bridge-deploy-script.sh --list                what is currently overridden
set -uo pipefail
DIR=/data/overrides
# Same trust anchor as the loader: read-only root first, /data only as a legacy fallback.
PUBKEY=/etc/netbridge/script-pubkey.pem
[ -f "$PUBKEY" ] || PUBKEY=/data/config/script-pubkey.pem
STAGE=/tmp/bridge-deploy.$$
log()  { echo "bridge-deploy: $*"; }
die()  { echo "bridge-deploy: ERROR $*" >&2; rm -rf "$STAGE"; exit "${2:-1}"; }
trap 'rm -rf "$STAGE"' EXIT

# Which services a script belongs to, so a deploy can restart the right thing. A script with
# no mapping is installed but nothing is restarted (harmless: it takes effect on next start).
svc_for() {
  case "$1" in
    bridge-return-audio.sh) echo "bridge-return-audio" ;;
    bridge-feeder-audio.sh) echo "bridge-feeder-audio" ;;
    bridge-feeder-net.sh)   echo "bridge-feeder-net" ;;
    bridge-uvcd.sh)         echo "bridge-uvcd" ;;
    *) echo "" ;;
  esac
}

case "${1:-}" in
  --list)
    [ -d "$DIR" ] || { echo "no overrides"; exit 0; }
    for f in "$DIR"/*.sh; do
      [ -f "$f" ] || continue
      n="$(basename "$f")"
      if openssl dgst -sha256 -verify "$PUBKEY" -signature "$f.sig" "$f" >/dev/null 2>&1; then
        echo "  $n  sha256=$(sha256sum "$f" | cut -c1-16)  signature OK"
      else
        echo "  $n  UNVERIFIED (will not run)"
      fi
    done
    [ -f "$DIR/.quarantined.json" ] && echo "  quarantined: $(cat "$DIR/.quarantined.json")"
    exit 0 ;;
  --revert)
    NAME="${2:?--revert needs a script name}"
    case "$NAME" in */*|.*) die "bad name" 64 ;; esac
    rm -f "$DIR/$NAME" "$DIR/$NAME.sig" "$DIR/.state/$NAME.starts"
    log "reverted $NAME to the baked-in version"
    s="$(svc_for "$NAME")"; [ -n "$s" ] && { systemctl restart "$s" && log "restarted $s"; }
    exit 0 ;;
  --unquarantine)
    # Put a quarantined override BACK without touching the card.
    #
    # Auto-rollback moves a suspect override into quarantine/ and the device silently keeps
    # running its baked-in script. Until now the ONLY way back was physically mounting the
    # SD card — so for a bridge in another city it stays reverted indefinitely, with one log
    # line as the only trace. That is a recovery gap, not a safety feature: the quarantine
    # did its job, and an operator who has since fixed the cause needs a way to re-arm it.
    #
    # The signature is re-verified before anything is restored, so a quarantined file that
    # was tampered with while it sat there still cannot come back. The start counter is
    # cleared too, so the restored override gets a genuinely fresh trial instead of resuming
    # one strike away from tripping again.
    NAME="${2:-}"
    Q="$DIR/quarantine"
    [ -d "$Q" ] || die "nothing quarantined" 0
    n=0
    for f in "$Q"/*; do
      [ -f "$f" ] || continue
      case "$f" in *.sig.*) continue ;; esac
      b="$(basename "$f")"; base="${b%%.[0-9]*}"
      [ -n "$NAME" ] && [ "$base" != "$NAME" ] && continue
      sig="$Q/${base}.sig.${b##*.}"
      [ -f "$sig" ] || { log "no signature kept for $base — refusing to restore"; continue; }
      if ! openssl dgst -sha256 -verify "$PUBKEY" -signature "$sig" "$f" >/dev/null 2>&1; then
        log "$base FAILED signature re-verify — leaving it quarantined"; continue
      fi
      install -m 0755 "$f" "$DIR/$base" && cp -f "$sig" "$DIR/$base.sig" || continue
      rm -f "$DIR/.state/$base.starts"
      rm -f "$f" "$sig"
      log "restored $base from quarantine (signature re-verified)"
      s="$(svc_for "$base")"; [ -n "$s" ] && { systemctl restart "$s" && log "restarted $s"; }
      n=$((n+1))
    done
    [ "$n" -gt 0 ] || die "nothing restored" 0
    rm -f "$DIR/.quarantined.json"
    log "unquarantine complete ($n restored)"
    exit 0 ;;
  --running)
    # Report WHICH code each service is actually executing. An operator otherwise cannot
    # tell an override from the baked-in script without pulling a diagnostics bundle —
    # which is exactly how three wrong conclusions got drawn about this bridge in one night.
    for n in bridge-return-audio.sh bridge-feeder-audio.sh bridge-feeder-net.sh bridge-uvcd.sh; do
      if [ -f "$DIR/$n" ] && openssl dgst -sha256 -verify "$PUBKEY" -signature "$DIR/$n.sig" "$DIR/$n" >/dev/null 2>&1; then
        echo "$n override sha256=$(sha256sum "$DIR/$n" | cut -c1-12)"
      elif [ -f "$DIR/$n" ]; then
        echo "$n override-UNVERIFIED (baked-in runs)"
      else
        echo "$n baked-in sha256=$(sha256sum "/usr/local/bin/$n" 2>/dev/null | cut -c1-12)"
      fi
    done
    q="$(ls "$DIR/quarantine" 2>/dev/null | grep -v '\.sig\.' | tr '\n' ' ')"
    [ -n "$q" ] && echo "quarantined: $q"
    exit 0 ;;
esac

NAME="${1:?usage: bridge-deploy-script.sh <name> <url-base> | --revert <name> | --list}"
SRC="${2:?need a url base to fetch <name> and <name>.sig from}"
case "$NAME" in */*|.*) die "bad name" 64 ;; esac
case "$SRC" in http://*|https://*) : ;; *) die "url base must be http(s)" 64 ;; esac
[ -f "$PUBKEY" ] || die "no pubkey at $PUBKEY — cannot verify, refusing" 2

mkdir -p "$STAGE" "$DIR" /data/config
log "fetching $NAME + signature from $SRC"
curl -fsS -m 60 -o "$STAGE/$NAME"     "$SRC/$NAME"     || die "download failed: $NAME" 3
curl -fsS -m 60 -o "$STAGE/$NAME.sig" "$SRC/$NAME.sig" || die "download failed: $NAME.sig" 3

log "verifying signature (EC/SHA256)"
openssl dgst -sha256 -verify "$PUBKEY" -signature "$STAGE/$NAME.sig" "$STAGE/$NAME" >/dev/null 2>&1 \
  || die "SIGNATURE VERIFY FAILED — refusing to install" 4
log "  signature OK  sha256=$(sha256sum "$STAGE/$NAME" | cut -c1-16)"

# A shell script that cannot even parse would crash-loop until the trip fires; catching it
# here costs nothing and keeps the bridge from ever entering that state.
head -c2 "$STAGE/$NAME" | grep -q '#!' || die "payload has no shebang — not a script" 5
bash -n "$STAGE/$NAME" 2>/dev/null || die "payload fails syntax check — refusing to install" 5
log "  syntax OK"

install -m 0755 "$STAGE/$NAME"     "$DIR/$NAME"
install -m 0644 "$STAGE/$NAME.sig" "$DIR/$NAME.sig"
rm -f "$DIR/.state/$NAME.starts" "$DIR/.quarantined.json"   # fresh trial for the new version
log "installed $DIR/$NAME"

s="$(svc_for "$NAME")"
if [ -n "$s" ]; then
  log "restarting $s"
  systemctl restart "$s" || die "service restart failed" 6
  sleep 3
  st="$(systemctl is-active "$s" 2>/dev/null)"
  log "  $s is $st"
  [ "$st" = "active" ] || die "service did not come back active — auto-rollback will trip if it keeps failing" 7
else
  log "no service mapped for $NAME — takes effect at next start"
fi
log "done"
