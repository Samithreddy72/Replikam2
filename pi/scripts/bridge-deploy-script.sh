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
