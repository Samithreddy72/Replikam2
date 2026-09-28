#!/bin/bash
# bridge-overrides.sh — puts signed updates in place and takes them away again.
#
# The signed-update engine for every file in /etc/netbridge/updatable.conf that is not one of
# the four media scripts bridge-run.sh loads itself. It is deliberately NOT updatable: it is the
# thing that reverts a bad update, so a bad update must never be able to replace it.
#
#   apply-all          boot: verify every stored override and put it in place (safe mode after
#                      3 boots that never became healthy: nothing is applied)
#   bind <name>        verify one override and put it in place now
#   unbind <name>      take it away (the built-in file is visible again)
#   policy <name> [--now]   make a newly installed file take effect, or queue it as pending
#   health             timer, every 30 s: healthy-boot mark, crash-loop rollback, lifeline
#                      guard, pending changes, status file
#   quarantine <name> <why> / revert <name> / revert-all
#   stamp <name>       record that the stored update was installed on the OS running now
#   superseded <name>  exit 0 when the stored update was installed on a different OS
#   status             one line per overridden / pending / quarantined file (fleet `running`)
#
# Why bind mounts: the root filesystem is read-only, and a bind-mounted file is what EVERY
# caller sees — a service, the CLI, the agent, sudo — with no change to any of them. The file
# underneath is untouched, so taking the mount away is a complete revert.
set -uo pipefail

CATALOG="${BRIDGE_OVR_CATALOG:-/etc/netbridge/updatable.conf}"
DIR="${BRIDGE_OVR_DIR:-/data/overrides}"
PUBKEY="${BRIDGE_OVR_PUBKEY:-/etc/netbridge/script-pubkey.pem}"
RUN="${BRIDGE_OVR_RUN:-/run/bridge-overrides}"
DROPIN_ROOT="${BRIDGE_OVR_DROPIN_ROOT:-/run/systemd/system}"
MOUNTINFO="${BRIDGE_OVR_MOUNTINFO:-/proc/self/mountinfo}"
UDC_GLOB="${BRIDGE_OVR_UDC_GLOB:-/sys/class/udc/*/state}"
UPTIME_SRC="${BRIDGE_OVR_UPTIME:-/proc/uptime}"
AGENT_OK="${BRIDGE_OVR_AGENT_OK:-/run/bridge-agent/last-ok}"
SYSTEMCTL="${BRIDGE_OVR_SYSTEMCTL:-systemctl}"
MOUNT="${BRIDGE_OVR_MOUNT:-mount}"
UMOUNT="${BRIDGE_OVR_UMOUNT:-umount}"
LIVE_CMD="${BRIDGE_OVR_LIVE_CMD:-}"        # tests: a command whose success means "session live"
NOW_CMD="${BRIDGE_OVR_NOW_CMD:-date +%s}"  # tests: fixed clock
MTIME_CMD="${BRIDGE_OVR_MTIME_CMD:-stat -c %Y}"  # tests: macOS stat differs
IMAGE_VERSION="${BRIDGE_OVR_IMAGE_VERSION:-/etc/netbridge-image-version}"   # the running slot's own version
BOOT_LIMIT=3                                # boots that never became healthy -> safe mode
HEALTHY_AFTER_S=180                         # uptime + core services active = a healthy boot
LIFELINE_QUIET_S=900                        # no fleet contact this long -> revert lifelines
CRASH_N=3; CRASH_WINDOW_S=120               # restarts within the window -> quarantine
CORE_UNITS="bridge-uvcd bridge-feeder-net bridge-feeder-audio bridge-web"

log(){ echo "bridge-overrides: $*" >&2; command -v logger >/dev/null 2>&1 && logger -t bridge-overrides "$*" 2>/dev/null; return 0; }
now(){ $NOW_CMD; }
uptime_s(){ cut -d' ' -f1 "$UPTIME_SRC" 2>/dev/null | cut -d. -f1; }

# row <name> -> "target kind apply units" (empty if not in the catalog)
row(){
  [ -n "${1:-}" ] || return 1
  awk -v n="$1" '!/^[[:space:]]*#/ && NF>=5 && $1==n {print $2, $3, $4, $5; f=1; exit} END{exit !f}' "$CATALOG"
}
names(){ awk '!/^[[:space:]]*#/ && NF>=5 {print $1}' "$CATALOG"; }
field(){ row "$1" | cut -d' ' -f"$2"; }
units_of(){ local u; u="$(field "$1" 4)"; [ "$u" = "-" ] && u=""; echo "$u"; }

verified(){ [ -f "$DIR/$1" ] && [ -f "$DIR/$1.sig" ] && [ -f "$PUBKEY" ] &&
            openssl dgst -sha256 -verify "$PUBKEY" -signature "$DIR/$1.sig" "$DIR/$1" >/dev/null 2>&1; }
sha(){ sha256sum "$1" 2>/dev/null | cut -c1-12; }

is_bound(){ awk -v t="$1" '$5==t {f=1} END {exit !f}' "$MOUNTINFO" 2>/dev/null; }

# ---------------------------------------------------------------- which OS an update belongs to
# An update is a replacement for ONE OS's built-in file (2026-09-28). /data survives an OS update
# (A/B), so every stored update used to be put back on top of the NEW OS at its first boot: a
# bridge-web.py hotfix for 2.2.0 kept running on 2.2.1 and silently undid 2.2.1's own changes, an
# old bridge-update.sh ran the next OS update, and the trial boot was judged on that mix. Each
# update now records the OS it was installed on (<name>.image), and on any other OS it stays on
# /data unused - the new OS's built-in runs. Going back to the old OS (a rollback) uses it again;
# deploying it again adopts it on the new OS. An update with no record was installed before this
# existed, i.e. on an older OS. bridge-run.sh asks `superseded` too, so the four media scripts it
# loads follow the same rule as the files bound here. Kept across OS updates on purpose:
#   keys  a rotated owner SSH key must not come back as the old one after an OS update
#   audio the voice/return pipeline and its drop-ins: the owner's standing rule is that the
#         audio pipeline is not changed, so an OS update carries these exactly as it always did
running_image(){ head -n1 "$IMAGE_VERSION" 2>/dev/null | tr -d '[:space:]'; }
installed_on(){ head -n1 "$DIR/$1.image" 2>/dev/null | tr -d '[:space:]'; }
carries_across_os(){
  [ "$(field "$1" 2)" = keys ] && return 0
  case "$1" in
    bridge-feeder-audio.sh|bridge-return-audio.sh|dropin.bridge-feeder-audio|dropin.bridge-return-audio) return 0 ;;
  esac
  return 1
}
superseded(){   # the stored update was installed on another OS than the one running
  local cur
  carries_across_os "$1" && return 1
  cur="$(running_image)"; [ -n "$cur" ] || return 1      # OS unknown: apply as before
  [ "$(installed_on "$1")" != "$cur" ]                   # unrecorded = installed before 2026-09-28
}
stamp(){
  local cur; cur="$(running_image)"
  if [ -n "$cur" ]; then printf '%s\n' "$cur" > "$DIR/$1.image"; else rm -f "$DIR/$1.image"; fi
  return 0
}
dropin_path(){ echo "$DROPIN_ROOT/$1.service.d/50-netbridge-override.conf"; }

session_live(){
  if [ -n "$LIVE_CMD" ]; then eval "$LIVE_CMD"; return; fi
  # Same test jitter-sentry uses: the video feeder is burning CPU = presenter video is flowing.
  local pid t0 t1
  pid=$(pgrep -f 'udpsrc port=5000' | head -1); [ -n "$pid" ] || return 1
  t0=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null) || return 1
  sleep 1
  t1=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null) || return 1
  [ "${t1:-0}" -gt "${t0:-0}" ]
}
laptop_attached(){
  local f
  for f in $UDC_GLOB; do [ "$(cat "$f" 2>/dev/null)" = "configured" ] && return 0; done
  return 1
}

# ---------------------------------------------------------------- putting files in place
detach_target(){
  local target="$1" attempts=0
  while is_bound "$target"; do
    attempts=$((attempts+1))
    [ "$attempts" -le 16 ] || { log "too many stacked mounts at $target"; return 5; }
    # Running shells may hold the old script open. Lazy detach changes future opens;
    # the existing process stays alive until the normal restart/idle policy permits it.
    "$UMOUNT" "$target" 2>/dev/null || "$UMOUNT" -l "$target" 2>/dev/null \
      || { log "cannot detach override at $target; no rollback claimed"; return 5; }
  done
}
bind_one(){   # verify + bind a bind/keys item; dropins are written under /run
  local name="$1" target kind
  target="$(field "$name" 1)"; kind="$(field "$name" 2)"
  case "$kind" in
    bind|keys) ;;
    dropin) dropin_one "$name"; return ;;
    *) log "$name: kind '$kind' is not bind-mounted"; return 2 ;;
  esac
  verified "$name" || { log "$name: override missing or signature invalid — not applied"; return 3; }
  [ -e "$target" ] || { log "$name: built-in $target does not exist — refusing"; return 4; }
  detach_target "$target" || return 5
  $MOUNT --bind "$DIR/$name" "$target" || { log "$name: bind mount failed"; return 5; }
  $MOUNT -o remount,bind,ro "$target" 2>/dev/null || true
  log "$name: in place (sha $(sha "$DIR/$name"))"
}
dropin_one(){
  local name="$1" unit p
  unit="$(field "$name" 1)"
  verified "$name" || { log "$name: drop-in missing or signature invalid — not applied"; return 3; }
  p="$(dropin_path "$unit")"; mkdir -p "$(dirname "$p")"
  install -m 0644 "$DIR/$name" "$p" && $SYSTEMCTL daemon-reload && log "$name: drop-in for $unit in place"
}
unbind_one(){
  local name="$1" target kind
  target="$(field "$name" 1)"; kind="$(field "$name" 2)"
  case "$kind" in
    bind|keys) detach_target "$target" || return 5 ;;
    dropin) rm -f "$(dropin_path "$target")"; $SYSTEMCTL daemon-reload ;;
  esac
  return 0
}

# ---------------------------------------------------------------- making a change take effect
pending_set(){ mkdir -p "$DIR/.pending"; printf '%s\n' "$2" > "$DIR/.pending/$1"; }
pending_clear(){ rm -f "$DIR/.pending/$1"; }

restart_units(){ local u rc=0; for u in $1; do $SYSTEMCTL restart "$u" || rc=1; done; return $rc; }

# policy <name> [--now]  -> prints what happened; returns 0 unless a restart failed
policy(){
  local name="$1" force="${2:-}" apply units
  apply="$(field "$name" 3)"; units="$(units_of "$name")"
  case "$apply" in
    restart|lifeline)
      restart_units "$units" || { echo "restart of $units FAILED"; return 1; }
      pending_clear "$name"; echo "applied now (restarted $units)" ;;
    idle)
      if [ "$force" != "--now" ] && session_live; then
        pending_set "$name" "waits for the presenter session to end"
        echo "pending — will apply automatically when no presenter video is flowing"; return 0
      fi
      restart_units "$units" || { echo "restart of $units FAILED"; return 1; }
      pending_clear "$name"; echo "applied now (restarted $units)" ;;
    camera|video)
      if laptop_attached; then
        pending_set "$name" "waits for the meeting laptop to be unplugged"
        echo "pending — will apply automatically once the meeting laptop is unplugged (camera restart with a laptop attached reboots the Pi)"; return 0
      fi
      if session_live; then
        pending_set "$name" "waits for the presenter session to end"
        echo "pending — will apply automatically when no presenter video is flowing"; return 0
      fi
      if [ "$apply" = video ]; then
        $SYSTEMCTL stop bridge-uvcd; $SYSTEMCTL restart bridge-feeder-net || { echo "feeder restart FAILED"; return 1; }
        $SYSTEMCTL start bridge-uvcd || { echo "camera start FAILED"; return 1; }
        pending_clear "$name"; echo "applied now (camera stopped, video feeder restarted, camera started)"
      else
        restart_units "$units" || { echo "restart of $units FAILED"; return 1; }
        pending_clear "$name"; echo "applied now (restarted $units)"
      fi ;;
    boot)  pending_set "$name" "takes effect at the next reboot"; echo "installed — takes effect at the next reboot" ;;
    none|agent) pending_clear "$name"; echo "installed — takes effect the next time it runs" ;;
    *) echo "unknown apply policy '$apply'"; return 1 ;;
  esac
}

# ---------------------------------------------------------------- rollback
quarantine(){
  local name="$1" why="${2:-}" ts q units kind
  ts="$(now)"; q="$DIR/quarantine"; mkdir -p "$q"
  kind="$(field "$name" 2)"; units="$(units_of "$name")"
  if [ "$kind" != loader ]; then unbind_one "$name" || return 5; fi
  mv -f "$DIR/$name" "$q/$name.$ts" 2>/dev/null
  mv -f "$DIR/$name.sig" "$q/$name.sig.$ts" 2>/dev/null
  rm -f "$DIR/$name.image"                                   # a restore stamps it afresh
  pending_clear "$name"
  printf '{"script":"%s","why":"%s","ts":%s}\n' "$name" "$why" "$ts" > "$DIR/.quarantined.json"
  log "QUARANTINED $name — $why; built-in restored"
  if [ "$kind" != loader ]; then
    case "$(field "$name" 3)" in
      restart|lifeline|idle) restart_units "$units" ;;
    esac
  fi
  return 0
}

revert(){
  local name="$1" kind
  kind="$(field "$name" 2)"
  if superseded "$name"; then       # never in use on this OS: nothing to restart
    rm -f "$DIR/$name" "$DIR/$name.sig" "$DIR/$name.image" "$DIR/.state/$name.starts"
    pending_clear "$name"
    echo "reverted $name (it was installed on another OS and not in use here)"; return 0
  fi
  if [ "$kind" != loader ]; then unbind_one "$name" || return 5; fi
  rm -f "$DIR/$name" "$DIR/$name.sig" "$DIR/$name.image" "$DIR/.state/$name.starts"
  pending_clear "$name"
  echo "reverted $name to the built-in version — $(policy "$name")"
}

# ---------------------------------------------------------------- boot
apply_all(){
  mkdir -p "$RUN" "$DIR"
  local tries n name kind
  tries="$(cat "$DIR/.boot-attempts" 2>/dev/null)"; case "$tries" in ''|*[!0-9]*) tries=0 ;; esac
  if [ "$tries" -ge "$BOOT_LIMIT" ]; then
    : > "$RUN/safe-mode"
    log "SAFE MODE — $tries boots in a row never became healthy; every override stays OFF this boot"
    write_status; return 0
  fi
  echo $((tries + 1)) > "$DIR/.boot-attempts"; sync
  n=0; : > "$RUN/booted"
  for name in $(names); do
    kind="$(field "$name" 2)"
    [ -f "$DIR/$name" ] || continue
    if superseded "$name"; then
      log "$name: installed on OS $(installed_on "$name" | grep . || echo 'unknown (before 2026-09-28)'), this is $(running_image) — the built-in runs"
      continue
    fi
    case "$kind" in
      bind|keys|dropin) bind_one "$name" && { n=$((n+1)); echo "$name $(sha "$DIR/$name")" >> "$RUN/booted"; } ;;
      loader) verified "$name" && echo "$name $(sha "$DIR/$name")" >> "$RUN/booted" ;;   # bridge-run.sh uses it
    esac
  done
  # A fresh boot starts every service with whatever is in place now — new files and reverts
  # alike — so nothing that was waiting for a restart is pending any more.
  rm -rf "$DIR/.pending"
  log "boot: $n override(s) in place (boot attempt $((tries + 1)))"
  write_status
}

# ---------------------------------------------------------------- health (timer)
crash_check(){   # quarantine a bound/drop-in override whose service keeps failing
  local name="$1" u st nr ts f line keep=""
  for u in $(units_of "$name"); do
    st="$($SYSTEMCTL show -p ActiveState --value "$u" 2>/dev/null)"
    nr="$($SYSTEMCTL show -p NRestarts --value "$u" 2>/dev/null)"; case "$nr" in ''|*[!0-9]*) nr=0 ;; esac
    if [ "$st" = failed ]; then quarantine "$name" "$u failed"; return; fi
    ts="$(now)"; f="$RUN/nrestarts.$u"
    [ -f "$f" ] && while read -r line; do
      set -- $line; [ $((ts - $1)) -le "$CRASH_WINDOW_S" ] && keep="$keep$line
"
    done < "$f"
    keep="$keep$ts $nr
"
    printf '%s' "$keep" > "$f"
    set -- $(printf '%s' "$keep" | head -1)
    if [ $((nr - ${2:-$nr})) -ge "$CRASH_N" ]; then
      quarantine "$name" "$u restarted $((nr - ${2:-$nr})) times in ${CRASH_WINDOW_S}s"; rm -f "$f"; return
    fi
  done
}

health(){
  mkdir -p "$RUN"
  local up name kind apply u all_ok quiet age
  up="$(uptime_s)"; up="${up:-0}"
  # 1. a healthy boot resets the boot counter (safe mode needs 3 bad boots IN A ROW)
  #    What was in effect during a healthy boot becomes "known good". After a safe-mode boot
  #    turns out healthy, every override that was never part of a healthy boot is quarantined,
  #    so the next boot does not walk into the same three failures again.
  if [ "$up" -ge "$HEALTHY_AFTER_S" ] && [ -s "$DIR/.boot-attempts" ] && [ "$(cat "$DIR/.boot-attempts")" != 0 ]; then
    all_ok=1
    for u in $CORE_UNITS; do [ "$($SYSTEMCTL is-active "$u" 2>/dev/null)" = active ] || all_ok=0; done
    if [ "$all_ok" = 1 ]; then
      echo 0 > "$DIR/.boot-attempts"; log "boot marked healthy"
      if [ -e "$RUN/safe-mode" ]; then
        for name in $(names); do
          [ -f "$DIR/$name" ] || continue
          superseded "$name" && continue          # not in use on this OS: not a suspect
          grep -qx "$name $(sha "$DIR/$name")" "$DIR/.known-good" 2>/dev/null ||
            quarantine "$name" "never part of a healthy boot, and 3 boots in a row failed while it was installed"
        done
      else
        cp -f "$RUN/booted" "$DIR/.known-good" 2>/dev/null
      fi
    fi
  fi
  [ -e "$RUN/safe-mode" ] && { write_status; return 0; }
  # 2. crash loops (bound/drop-in services; bridge-run.sh already guards the loader four)
  for name in $(names); do
    [ -f "$DIR/$name" ] || continue
    superseded "$name" && continue            # not in place: its unit runs the built-in
    kind="$(field "$name" 2)"; apply="$(field "$name" 3)"
    case "$kind" in bind|dropin) ;; *) continue ;; esac
    case "$apply" in restart|lifeline|idle|camera|video) crash_check "$name" ;; esac
  done
  # 3. lifeline guard: an update to anything the bridge needs to stay reachable is reverted if
  #    the fleet has not heard from this bridge for 15 minutes since it went in.
  if [ "$up" -ge "$LIFELINE_QUIET_S" ]; then
    quiet=1
    if [ -f "$AGENT_OK" ]; then age=$(( $(now) - $($MTIME_CMD "$AGENT_OK" 2>/dev/null || echo 0) )); [ "$age" -lt "$LIFELINE_QUIET_S" ] && quiet=0; fi
    if [ "$quiet" = 1 ]; then
      for name in $(names); do
        [ -f "$DIR/$name" ] || continue
        superseded "$name" && continue
        case "$(field "$name" 3)" in agent|lifeline) ;; *) continue ;; esac
        age=$(( $(now) - $($MTIME_CMD "$DIR/$name" 2>/dev/null || echo 0) ))
        [ "$age" -ge "$LIFELINE_QUIET_S" ] && quarantine "$name" "no fleet contact for ${LIFELINE_QUIET_S}s after it was installed"
      done
    fi
  fi
  # 4. pending changes whose moment has come
  if [ -d "$DIR/.pending" ]; then
    for f in "$DIR/.pending"/*; do
      [ -f "$f" ] || continue
      name="$(basename "$f")"
      row "$name" >/dev/null || { rm -f "$f"; continue; }
      case "$(field "$name" 3)" in boot|none|agent) continue ;; esac
      out="$(policy "$name")"; case "$out" in applied*) log "$name: $out" ;; esac
    done
  fi
  write_status
}

# ---------------------------------------------------------------- reporting
status_lines(){
  local name kind state p
  [ -e "$RUN/safe-mode" ] && echo "SAFE MODE: overrides off this boot (3 unhealthy boots in a row)"
  for name in $(names); do
    [ -f "$DIR/$name" ] || [ -f "$DIR/.pending/$name" ] || continue   # built-in: nothing to say
    state=""
    if [ -f "$DIR/$name" ]; then
      if ! verified "$name"; then state="override-UNVERIFIED (built-in runs)"
      elif superseded "$name"; then state="override sha256=$(sha "$DIR/$name") NOT IN USE (installed on OS $(installed_on "$name" | grep . || echo '?'); built-in runs)"
      else state="override sha256=$(sha "$DIR/$name")"; fi
    fi
    p="$DIR/.pending/$name"; [ -f "$p" ] && state="${state:+$state; }PENDING: $(cat "$p")"
    [ -n "$state" ] && echo "$name $state"
  done
  local q; q="$(ls "$DIR/quarantine" 2>/dev/null | grep -v '\.sig\.' | tr '\n' ' ')"
  [ -n "$q" ] && echo "quarantined: $q"
  return 0
}
write_status(){
  mkdir -p "$RUN"
  { printf '{"safe_mode":%s,"boot_attempts":%s,"pending":[' \
      "$([ -e "$RUN/safe-mode" ] && echo true || echo false)" "$(cat "$DIR/.boot-attempts" 2>/dev/null || echo 0)"
    ls "$DIR/.pending" 2>/dev/null | awk 'BEGIN{s=""} {printf "%s\"%s\"", s, $0; s=","}'
    printf '],"active":['
    local s="" name old=""
    for name in $(names); do
      [ -f "$DIR/$name" ] && verified "$name" || continue
      if superseded "$name"; then old="$old${old:+,}\"$name\""; continue; fi
      printf '%s"%s"' "$s" "$name"; s=","
    done
    # installed on another OS and not in use here (the panel shows them as such)
    printf '],"superseded":[%s]}\n' "$old"; } > "$RUN/status.json.tmp" && mv -f "$RUN/status.json.tmp" "$RUN/status.json"
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  apply-all)  apply_all ;;
  bind)       row "${1:-}" >/dev/null || { echo "not updatable: ${1:-}" >&2; exit 64; }; bind_one "$1" ;;
  unbind)     row "${1:-}" >/dev/null || { echo "not updatable: ${1:-}" >&2; exit 64; }; unbind_one "$1" ;;
  policy)     row "${1:-}" >/dev/null || { echo "not updatable: ${1:-}" >&2; exit 64; }; policy "$1" "${2:-}" ;;
  quarantine) row "${1:-}" >/dev/null || exit 64; quarantine "$1" "${2:-manual}" ;;
  revert)     row "${1:-}" >/dev/null || { echo "not updatable: ${1:-}" >&2; exit 64; }; revert "$1" ;;
  stamp)      row "${1:-}" >/dev/null || { echo "not updatable: ${1:-}" >&2; exit 64; }; stamp "$1" ;;
  superseded) row "${1:-}" >/dev/null || exit 64; [ -f "$DIR/$1" ] && superseded "$1" ;;
  revert-all) failures=0; for n in $(names); do if [ -f "$DIR/$n" ]; then revert "$n" || failures=1; fi; done; write_status; exit "$failures" ;;
  health)     health ;;
  status)     status_lines ;;
  row)        row "${1:-}" ;;
  *) echo "usage: bridge-overrides.sh {apply-all|bind|unbind|policy|quarantine|revert|revert-all|stamp|superseded|health|status|row} [name]" >&2; exit 64 ;;
esac
