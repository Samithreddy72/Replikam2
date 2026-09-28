#!/bin/bash
# Install a signed update of one NetBridge file — no reflash — and make it take effect safely.
#
# Called by the fleet agent (commands deploy-script / revert-script / unquarantine / running)
# or by hand over SSH. It NEVER trusts the payload: the signature is verified here before
# anything is installed, and again at every use (bridge-run.sh at every service start,
# bridge-overrides.sh at every boot). Which files may be replaced, and how each one takes
# effect, is /etc/netbridge/updatable.conf on the read-only root — not a list a remote party
# can extend.
#
#   bridge-deploy-script.sh <name> <url-base> [--now]   fetch <name> + <name>.sig, verify, install
#   bridge-deploy-script.sh --revert <name>             back to the built-in file
#   bridge-deploy-script.sh --revert-all                every file back to built-in
#   bridge-deploy-script.sh --unquarantine [name]       restore a parked file (signature re-checked)
#   bridge-deploy-script.sh --running                   what every updatable file is running now
#   bridge-deploy-script.sh --list                      same, older name
#
# --now only skips the "wait until the presenter session ends" deferral for services outside
# the camera path. The camera is never restarted while the meeting laptop is attached: that
# hangs the USB controller and reboots the Pi (2026-09-24, 2/2) — the change waits instead.
set -uo pipefail
DIR="${BRIDGE_DEPLOY_DIR:-/data/overrides}"
# Same trust anchor as the loader: read-only root first, /data only as a legacy fallback.
PUBKEY="${BRIDGE_DEPLOY_PUBKEY:-/etc/netbridge/script-pubkey.pem}"
[ -f "$PUBKEY" ] || PUBKEY=/data/config/script-pubkey.pem
OVR="${BRIDGE_DEPLOY_OVERRIDES:-/usr/local/bin/bridge-overrides.sh}"
BAKED_DIR="${BRIDGE_DEPLOY_BAKED_DIR:-/usr/local/bin}"
FETCH="${BRIDGE_DEPLOY_FETCH:-curl -fsS -m 60 --retry 3 --retry-delay 3 -o}"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/bridge-deploy.XXXXXX")"
log()  { echo "bridge-deploy: $*"; }
die()  { echo "bridge-deploy: ERROR $*" >&2; rm -rf "$STAGE"; exit "${2:-1}"; }
trap 'rm -rf "$STAGE"' EXIT

row()  { "$OVR" row "$1" 2>/dev/null; }          # "target kind apply units", empty if not updatable
bare() { case "$1" in ""|*/*|.*) return 1 ;; esac; return 0; }

# Type check before anything is installed. A file that cannot even be parsed never reaches a
# service: shell -> bash -n, Python -> compile, SSH keys -> ssh-keygen, unit drop-ins -> sections.
check_payload(){
  local f="$1" name="$2" kind="$3" first
  case "$kind" in
    keys)
      local n=0 line
      while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        printf '%s\n' "$line" | ssh-keygen -l -f /dev/stdin >/dev/null 2>&1 || return 1
        n=$((n+1))
      done < "$f"
      [ "$n" -gt 0 ] || return 1 ;;
    dropin)
      # at least one section, and only [Unit] / [Service] / [Install]
      grep -qE '^\[(Unit|Service|Install)\]$' "$f" || return 1
      grep -E '^\[' "$f" | grep -qvE '^\[(Unit|Service|Install)\]$' && return 1
      return 0 ;;
    *)
      first="$(head -c 128 "$f" | head -1)"
      case "$first" in '#!'*) ;; *) return 1 ;; esac
      case "$first" in
        *python*) python3 -c 'import py_compile,sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' \
                    "$f" "$STAGE/.pyc" >/dev/null 2>&1 || return 1 ;;
        *bash*|*/sh*) bash -n "$f" 2>/dev/null || return 1 ;;
        *) return 1 ;;
      esac ;;
  esac
  return 0
}

running(){
  local name
  # The four media scripts first, in the format this command has always printed.
  local line
  for name in bridge-return-audio.sh bridge-feeder-audio.sh bridge-feeder-net.sh bridge-uvcd.sh; do
    if [ -f "$DIR/$name" ] && openssl dgst -sha256 -verify "$PUBKEY" -signature "$DIR/$name.sig" "$DIR/$name" >/dev/null 2>&1; then
      line="$name override sha256=$(sha256sum "$DIR/$name" | cut -c1-12)"
      "$OVR" superseded "$name" 2>/dev/null && line="$line NOT IN USE (installed on another OS; baked-in runs)"
    elif [ -f "$DIR/$name" ]; then
      line="$name override-UNVERIFIED (baked-in runs)"
    else
      line="$name baked-in sha256=$(sha256sum "$BAKED_DIR/$name" 2>/dev/null | cut -c1-12)"
    fi
    [ -f "$DIR/.pending/$name" ] && line="$line; PENDING: $(cat "$DIR/.pending/$name")"
    echo "$line"
  done
  # Everything else only when it is not simply the built-in file.
  "$OVR" status 2>/dev/null | grep -v -E '^(bridge-return-audio|bridge-feeder-audio|bridge-feeder-net|bridge-uvcd)\.sh '
  return 0
}

case "${1:-}" in
  --running|--list) running; exit 0 ;;
  --revert)
    NAME="${2:?--revert needs a name}"; bare "$NAME" || die "bad name" 64
    [ -n "$(row "$NAME")" ] || die "$NAME is not an updatable file" 64
    "$OVR" revert "$NAME"; exit $? ;;
  --revert-all)
    "$OVR" revert-all; exit $? ;;
  --unquarantine)
    NAME="${2:-}"
    Q="$DIR/quarantine"
    [ -d "$Q" ] || die "nothing quarantined" 0
    n=0
    for f in "$Q"/*; do
      [ -f "$f" ] || continue
      case "$f" in *.sig.*) continue ;; esac
      b="$(basename "$f")"; base="${b%.*}"; ts="${b##*.}"
      case "$ts" in *[!0-9]*) continue ;; esac
      [ -n "$NAME" ] && [ "$base" != "$NAME" ] && continue
      [ -n "$(row "$base")" ] || { log "$base is no longer updatable — leaving it parked"; continue; }
      sig="$Q/${base}.sig.${ts}"
      [ -f "$sig" ] || { log "no signature kept for $base — refusing to restore"; continue; }
      if ! openssl dgst -sha256 -verify "$PUBKEY" -signature "$sig" "$f" >/dev/null 2>&1; then
        log "$base FAILED signature re-verify — leaving it quarantined"; continue
      fi
      kind="$(row "$base" | cut -d' ' -f2)"
      mode=0755; case "$kind" in keys|dropin) mode=0644 ;; esac
      install -m "$mode" "$f" "$DIR/$base" && cp -f "$sig" "$DIR/$base.sig" || continue
      "$OVR" stamp "$base" >/dev/null 2>&1        # restored on purpose on THIS OS
      sync                                        # on the card before anything restarts
      rm -f "$DIR/.state/$base.starts" "$f" "$sig"
      case "$kind" in bind|keys|dropin) "$OVR" bind "$base" || { log "$base: could not be put in place"; continue; } ;; esac
      log "restored $base from quarantine (signature re-verified) — $("$OVR" policy "$base")"
      n=$((n+1))
    done
    [ "$n" -gt 0 ] || die "nothing restored" 0
    rm -f "$DIR/.quarantined.json"
    log "unquarantine complete ($n restored)"
    exit 0 ;;
esac

NAME="${1:?usage: bridge-deploy-script.sh <name> <url-base> [--now] | --revert <name> | --running}"
SRC="${2:?need a url base to fetch <name> and <name>.sig from}"
NOW="${3:-}"
bare "$NAME" || die "bad name" 64
case "$SRC" in http://*|https://*) : ;; *) die "url base must be http(s)" 64 ;; esac
case "$NOW" in ""|--now) ;; *) die "unknown option $NOW" 64 ;; esac
ROW="$(row "$NAME")"
[ -n "$ROW" ] || die "$NAME is not an updatable file (see /etc/netbridge/updatable.conf)" 64
KIND="$(echo "$ROW" | cut -d' ' -f2)"
[ -f "$PUBKEY" ] || die "no pubkey at $PUBKEY — cannot verify, refusing" 2
mkdir -p "$DIR"

log "fetching $NAME + signature from $SRC"
$FETCH "$STAGE/$NAME"     "$SRC/$NAME"     || die "download failed: $NAME" 3
$FETCH "$STAGE/$NAME.sig" "$SRC/$NAME.sig" || die "download failed: $NAME.sig" 3
log "verifying signature (EC/SHA256)"
openssl dgst -sha256 -verify "$PUBKEY" -signature "$STAGE/$NAME.sig" "$STAGE/$NAME" >/dev/null 2>&1 \
  || die "SIGNATURE VERIFY FAILED — refusing to install" 4
log "  signature OK  sha256=$(sha256sum "$STAGE/$NAME" | cut -c1-16)"
check_payload "$STAGE/$NAME" "$NAME" "$KIND" || die "payload fails its $KIND check — refusing to install" 5
log "  $KIND check OK"

MODE=0755; case "$KIND" in keys|dropin) MODE=0644 ;; esac
install -m "$MODE" "$STAGE/$NAME"     "$DIR/$NAME"
install -m 0644    "$STAGE/$NAME.sig" "$DIR/$NAME.sig"
# Installed for the OS running now: after an OS update it stays unused until deployed again
# (bridge-overrides.sh, "which OS an update belongs to").
"$OVR" stamp "$NAME" >/dev/null 2>&1
rm -f "$DIR/.state/$NAME.starts" "$DIR/.quarantined.json"   # a fresh trial for the new version
sync                                   # on the card BEFORE anything restarts: a reset seconds
                                       # later once left 0-byte files behind (2026-09-24)
log "installed $DIR/$NAME"
case "$KIND" in
  bind|keys|dropin) "$OVR" bind "$NAME" || die "could not put $NAME in place (the built-in file stays in use)" 6 ;;
esac
RES="$("$OVR" policy "$NAME" $NOW)" || die "$RES" 7
log "$NAME: $RES"
case "$RES" in
  applied*)
    sleep "${BRIDGE_DEPLOY_SETTLE_S:-3}"
    UNITS="$(echo "$ROW" | cut -d' ' -f4)"; [ "$UNITS" = "-" ] && UNITS=""
    [ "$(echo "$ROW" | cut -d' ' -f3)" = video ] && UNITS="bridge-feeder-net bridge-uvcd"
    for u in $UNITS; do
      if ${BRIDGE_DEPLOY_SYSTEMCTL:-systemctl} is-failed --quiet "$u" 2>/dev/null; then
        die "$u FAILED after the restart — automatic rollback will park the new file if it keeps failing" 7
      fi
      log "  $u: $(${BRIDGE_DEPLOY_SYSTEMCTL:-systemctl} is-active "$u" 2>/dev/null)"
    done ;;
esac
log "done"
