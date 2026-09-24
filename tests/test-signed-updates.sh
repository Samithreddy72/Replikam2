#!/bin/bash
# Signed updates beyond the four media scripts (2026-09-24): the catalog, the installer, the
# bind-mount engine, "when does it take effect", and every automatic rollback — WITHOUT a Pi.
#
# The real scripts run against a sandbox: real openssl keys and signatures, the real catalog
# (pi/configs/updatable.conf), and stand-ins only for what needs a Pi (mount, umount, systemctl,
# the UDC state, the clock, "is a presenter session live").
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE/.."
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); }

mkdir -p "$T/bin" "$T/server" "$T/data/overrides" "$T/run" "$T/dropins" "$T/state" "$T/root/usr/local/bin" "$T/root/home/pi" "$T/root/etc/netbridge"
openssl ecparam -name prime256v1 -genkey -noout -out "$T/key.pem" 2>/dev/null
openssl ec -in "$T/key.pem" -pubout -out "$T/pub.pem" 2>/dev/null
openssl ecparam -name prime256v1 -genkey -noout -out "$T/evil.pem" 2>/dev/null

# The catalog, with every target moved into the sandbox root.
sed -E "s#(^[^#][^ ]+[[:space:]]+)(/usr/local/bin|/home/pi|/etc/netbridge)#\1$T/root\2#" \
  "$REPO/pi/configs/updatable.conf" > "$T/updatable.conf"
for n in $(awk '!/^[[:space:]]*#/ && NF>=5 && $3!="dropin" {print $2}' "$T/updatable.conf"); do
  printf '#!/bin/bash\necho BUILTIN\n' > "$n"; chmod +x "$n"
done

# ---- stand-ins for the Pi ------------------------------------------------------------------
: > "$T/mountinfo"; : > "$T/systemctl.log"
cat > "$T/bin/sha256sum" <<'EOF'
#!/bin/bash
shasum -a 256 "$@"
EOF
cat > "$T/bin/mount" <<EOF
#!/bin/bash
if [ "\$1" = "--bind" ]; then echo "1 1 0:0 / \$3 rw - x x rw src=\$2" >> "$T/mountinfo"; fi
exit 0
EOF
cat > "$T/bin/umount" <<EOF
#!/bin/bash
awk -v t="\$1" 'BEGIN{d=0} {l[NR]=\$0; m[NR]=\$5} END{for(i=NR;i>=1;i--) if(!d && m[i]==t){skip=i; d=1}; for(i=1;i<=NR;i++) if(i!=skip) print l[i]}' "$T/mountinfo" > "$T/mountinfo.new"
mv "$T/mountinfo.new" "$T/mountinfo"; exit 0
EOF
cat > "$T/bin/systemctl" <<EOF
#!/bin/bash
echo "\$*" >> "$T/systemctl.log"
st(){ cat "$T/state/\$1.active" 2>/dev/null || echo active; }
case "\$1" in
  show) [ "\$3" = ActiveState ] && st "\$5"; [ "\$3" = NRestarts ] && { cat "$T/state/\$5.nrestarts" 2>/dev/null || echo 0; }; exit 0 ;;
  is-active) st "\$2"; [ "\$(st "\$2")" = active ] ;;
  is-failed) [ "\$(st "\$3")" = failed ] ;;
  *) exit 0 ;;
esac
EOF
cat > "$T/fetch.sh" <<EOF
#!/bin/bash
cp "$T/server/\$(basename "\$2")" "\$1" 2>/dev/null
EOF
chmod +x "$T"/bin/* "$T/fetch.sh"
echo 5000.00 > "$T/uptime"; echo "not attached" > "$T/udc"
: > "$T/live"                         # empty = not live; "1" = live
export PATH="$T/bin:$PATH"
export BRIDGE_OVR_CATALOG="$T/updatable.conf" BRIDGE_OVR_DIR="$T/data/overrides" BRIDGE_OVR_PUBKEY="$T/pub.pem" \
       BRIDGE_OVR_RUN="$T/run" BRIDGE_OVR_DROPIN_ROOT="$T/dropins" BRIDGE_OVR_MOUNTINFO="$T/mountinfo" \
       BRIDGE_OVR_UDC_GLOB="$T/udc" BRIDGE_OVR_UPTIME="$T/uptime" BRIDGE_OVR_AGENT_OK="$T/agent-ok" \
       BRIDGE_OVR_SYSTEMCTL="$T/bin/systemctl" BRIDGE_OVR_MOUNT="$T/bin/mount" BRIDGE_OVR_UMOUNT="$T/bin/umount" \
       BRIDGE_OVR_LIVE_CMD="[ -s $T/live ]" BRIDGE_OVR_MTIME_CMD="stat -f %m"
export BRIDGE_DEPLOY_DIR="$T/data/overrides" BRIDGE_DEPLOY_PUBKEY="$T/pub.pem" \
       BRIDGE_DEPLOY_OVERRIDES="$REPO/pi/scripts/bridge-overrides.sh" BRIDGE_DEPLOY_BAKED_DIR="$T/root/usr/local/bin" \
       BRIDGE_DEPLOY_FETCH="$T/fetch.sh" BRIDGE_DEPLOY_SYSTEMCTL="$T/bin/systemctl" BRIDGE_DEPLOY_SETTLE_S=0
OVR="$REPO/pi/scripts/bridge-overrides.sh"; DEPLOY="$REPO/pi/scripts/bridge-deploy-script.sh"
D="$T/data/overrides"

publish(){  # publish <name> <file-with-content> [key]
  cp "$2" "$T/server/$1"; openssl dgst -sha256 -sign "${3:-$T/key.pem}" -out "$T/server/$1.sig" "$T/server/$1" 2>/dev/null; }
deploy(){ bash "$DEPLOY" "$@" https://fleet.example/payloads "${DEPLOY_NOW:-}" >"$T/out" 2>&1; echo $? > "$T/rc"; }
deploy_now(){ bash "$DEPLOY" "$1" https://fleet.example/payloads --now >"$T/out" 2>&1; echo $? > "$T/rc"; }
rc(){ cat "$T/rc"; }
bound(){ awk -v t="$1" '$5==t {f=1} END {exit !f}' "$T/mountinfo"; }
called(){ grep -qx "$1" "$T/systemctl.log"; }
reset_log(){ : > "$T/systemctl.log"; }

# ===================== 1. the catalog is the only list =====================
bash "$OVR" row bridge-web.py | grep -q ' bind restart bridge-web$' && ok "catalog: bridge-web.py = bind / restart bridge-web" || no "catalog lookup wrong: $(bash "$OVR" row bridge-web.py)"
printf '#!/bin/bash\necho EVIL\n' > "$T/evil.sh"
for n in bridge-run.sh bridge-deploy-script.sh bridge-overrides.sh bridge-ab bridge-pin; do
  publish "$n" "$T/evil.sh"; deploy "$n"
  [ "$(rc)" = 64 ] && [ ! -e "$D/$n" ] || { no "safety-net file $n was accepted (rc $(rc))"; continue; }
done
ok "the safety net (loader, installer, rollback, A/B, PIN) cannot be replaced remotely"
deploy "../etc/passwd"; [ "$(rc)" = 64 ] && ok "path traversal refused" || no "path traversal accepted"

# ===================== 2. a Python service update: verified, compiled, bound, restarted =====================
printf '#!/usr/bin/env python3\nprint("NEW STATUS PAGE")\n' > "$T/web.py"
publish bridge-web.py "$T/web.py"; reset_log; deploy bridge-web.py
if [ "$(rc)" = 0 ] && bound "$T/root/usr/local/bin/bridge-web.py" && called "restart bridge-web" && grep -q "applied now" "$T/out"; then
  ok "bridge-web.py: signature OK, compiles, bind-mounted over the built-in, bridge-web restarted"
else no "bridge-web.py deploy: rc=$(rc) $(tail -3 "$T/out")"; fi
[ "$(stat -f %Lp "$D/bridge-web.py")" = 755 ] && ok "installed executable (0755)" || no "wrong mode $(stat -f %Lp "$D/bridge-web.py")"

printf '#!/usr/bin/env python3\nprint("broken"\n' > "$T/bad.py"
publish jitter-sentry.sh "$T/evil.sh"                   # (valid bash — used later)
publish bridge-jitter.py "$T/bad.py"; deploy bridge-jitter.py
[ "$(rc)" = 5 ] && [ ! -e "$D/bridge-jitter.py" ] && ok "Python with a syntax error is refused before install" || no "broken Python accepted (rc $(rc))"
printf '#!/bin/bash\nif then\n' > "$T/bad.sh"; publish bridge-status.sh "$T/bad.sh"; deploy bridge-status.sh
[ "$(rc)" = 5 ] && ok "shell with a syntax error is refused before install" || no "broken shell accepted (rc $(rc))"
publish bridge-read.py "$T/web.py" "$T/evil.pem"; deploy bridge-read.py
[ "$(rc)" = 4 ] && [ ! -e "$D/bridge-read.py" ] && ok "signed with the WRONG key -> refused" || no "wrong-key payload accepted (rc $(rc))"
publish bridge-read.py "$T/web.py"; printf '#!/usr/bin/env python3\nprint("tampered")\n' > "$T/server/bridge-read.py"; deploy bridge-read.py
[ "$(rc)" = 4 ] && ok "tampered after signing -> refused" || no "tampered payload accepted (rc $(rc))"

# ===================== 3. the camera never restarts under an attached laptop =====================
printf '#!/bin/bash\necho CAMERA-V2\n' > "$T/uvcd.sh"; publish bridge-uvcd.sh "$T/uvcd.sh"
echo configured > "$T/udc"; reset_log; deploy bridge-uvcd.sh
if [ "$(rc)" = 0 ] && ! called "restart bridge-uvcd" && [ -f "$D/.pending/bridge-uvcd.sh" ] && grep -q "unplugged" "$T/out"; then
  ok "camera update with the laptop attached: installed, NOT restarted, pending"
else no "camera update under attached laptop: rc=$(rc) $(tail -2 "$T/out")"; fi
bash "$DEPLOY" --running | grep -q '^bridge-uvcd.sh override sha256=.*PENDING: waits for the meeting laptop' \
  && ok "fleet 'running' shows it as pending, with the reason" || no "running output: $(bash "$DEPLOY" --running)"
reset_log; bash "$OVR" health >/dev/null 2>&1
! called "restart bridge-uvcd" && ok "health tick: laptop still attached -> still waits" || no "camera restarted while attached"
echo "not attached" > "$T/udc"; echo 1 > "$T/live"; reset_log; bash "$OVR" health >/dev/null 2>&1
! called "restart bridge-uvcd" && ok "health tick: unplugged but a session is live -> still waits" || no "camera restarted during a live session"
: > "$T/live"; reset_log; bash "$OVR" health >/dev/null 2>&1
called "restart bridge-uvcd" && [ ! -f "$D/.pending/bridge-uvcd.sh" ] && ok "health tick: unplugged + idle -> applied, pending cleared" || no "pending camera update never applied"

# ===================== 4. the video feeder uses the safe sequence =====================
printf '#!/bin/bash\necho FEEDER-V2\n' > "$T/feeder.sh"; publish bridge-feeder-net.sh "$T/feeder.sh"; reset_log; deploy bridge-feeder-net.sh
if [ "$(sed -n 1,3p "$T/systemctl.log" | tr '\n' '|')" = "stop bridge-uvcd|restart bridge-feeder-net|start bridge-uvcd|" ]; then
  ok "video feeder update: stop camera -> restart feeder -> start camera (never the feeder alone)"
else no "feeder sequence was: $(tr '\n' '|' < "$T/systemctl.log")"; fi

# ===================== 5. audio waits for the session to end; --now overrides =====================
printf '#!/bin/bash\necho VOICE-V2\n' > "$T/voice.sh"; publish bridge-feeder-audio.sh "$T/voice.sh"
echo 1 > "$T/live"; reset_log; deploy bridge-feeder-audio.sh
! called "restart bridge-feeder-audio" && [ -f "$D/.pending/bridge-feeder-audio.sh" ] && ok "voice update during a live session: pending, audio not restarted" || no "voice restarted mid-session"
reset_log; deploy_now bridge-feeder-audio.sh
called "restart bridge-feeder-audio" && [ ! -f "$D/.pending/bridge-feeder-audio.sh" ] && ok "--now applies it immediately when asked" || no "--now did not apply"
: > "$T/live"

# ===================== 6. boot-time files, SSH keys, unit drop-ins =====================
printf '#!/bin/bash\necho GADGET-V2\n' > "$T/gadget.sh"; publish uvc-raw-setup.sh "$T/gadget.sh"; reset_log; deploy uvc-raw-setup.sh
[ "$(rc)" = 0 ] && bound "$T/root/home/pi/uvc-raw-setup.sh" && ! grep -q restart "$T/systemctl.log" && grep -q "next reboot" "$T/out" \
  && ok "USB descriptor update: in place, nothing restarted, applies at the next reboot" || no "uvc-raw-setup.sh: rc=$(rc) $(tail -2 "$T/out")"
ssh-keygen -q -t ed25519 -N "" -f "$T/k1" && printf 'restrict,pty %s\n' "$(cat "$T/k1.pub")" > "$T/keys"
publish owner_ssh_authorized_keys "$T/keys"; deploy owner_ssh_authorized_keys
[ "$(rc)" = 0 ] && bound "$T/root/etc/netbridge/owner_ssh_authorized_keys" && [ "$(stat -f %Lp "$D/owner_ssh_authorized_keys")" = 644 ] \
  && ok "owner SSH key rotation: valid key accepted, 0644, in place" || no "key rotation failed: rc=$(rc) $(tail -2 "$T/out")"
printf 'ssh-ed25519 NOT-A-KEY\n' > "$T/badkeys"; publish owner_ssh_authorized_keys "$T/badkeys"; deploy owner_ssh_authorized_keys
[ "$(rc)" = 5 ] && ok "a malformed key file is refused (cannot lock the owner out by typo)" || no "malformed key accepted (rc $(rc))"
printf '[Service]\nCPUAffinity=3\n' > "$T/dropin"; publish dropin.bridge-web "$T/dropin"; reset_log; deploy dropin.bridge-web
[ "$(rc)" = 0 ] && [ -f "$T/dropins/bridge-web.service.d/50-netbridge-override.conf" ] && called "daemon-reload" && called "restart bridge-web" \
  && ok "systemd drop-in: written under /run, daemon-reload, unit restarted" || no "drop-in: rc=$(rc) $(tail -2 "$T/out")"
printf '[Evil]\nx=1\n' > "$T/dropin2"; publish dropin.jitter-sentry "$T/dropin2"; deploy dropin.jitter-sentry
[ "$(rc)" = 5 ] && ok "drop-in with an unknown section is refused" || no "bad drop-in accepted (rc $(rc))"

# ===================== 7. revert =====================
reset_log; bash "$DEPLOY" --revert bridge-web.py >"$T/out" 2>&1
! bound "$T/root/usr/local/bin/bridge-web.py" && [ ! -e "$D/bridge-web.py" ] && called "restart bridge-web" \
  && ok "revert: unmounted, removed, service restarted on the built-in" || no "revert failed: $(cat "$T/out")"

# ===================== 8. crash-loop rollback of a bound service =====================
publish bridge-web.py "$T/web.py"; deploy bridge-web.py
echo 1000 > "$T/nowv"; export BRIDGE_OVR_NOW_CMD="cat $T/nowv"
echo 0 > "$T/state/bridge-web.nrestarts"; bash "$OVR" health >/dev/null 2>&1
echo 1030 > "$T/nowv"; echo 2 > "$T/state/bridge-web.nrestarts"; bash "$OVR" health >/dev/null 2>&1
[ -e "$D/bridge-web.py" ] && ok "2 restarts in 30 s: not yet a crash loop" || no "quarantined too early"
echo 1060 > "$T/nowv"; echo 4 > "$T/state/bridge-web.nrestarts"; reset_log; bash "$OVR" health >/dev/null 2>&1
if [ ! -e "$D/bridge-web.py" ] && ls "$D/quarantine" | grep -q '^bridge-web.py\.' && ! bound "$T/root/usr/local/bin/bridge-web.py" && called "restart bridge-web"; then
  ok "4 restarts in 60 s: quarantined, unmounted, restarted on the built-in"
else no "crash loop not caught"; fi
grep -q '"script":"bridge-web.py"' "$D/.quarantined.json" && ok "quarantine recorded for the fleet" || no "no .quarantined.json"
publish jitter-sentry.sh "$T/evil.sh"; deploy jitter-sentry.sh
echo failed > "$T/state/jitter-sentry.active"; bash "$OVR" health >/dev/null 2>&1
[ ! -e "$D/jitter-sentry.sh" ] && ok "a service in 'failed' state: quarantined at once" || no "failed service not quarantined"
rm -f "$T/state/jitter-sentry.active"

# ===================== 9. unquarantine re-checks the signature and re-binds =====================
reset_log; bash "$DEPLOY" --unquarantine bridge-web.py >"$T/out" 2>&1
[ -e "$D/bridge-web.py" ] && bound "$T/root/usr/local/bin/bridge-web.py" && called "restart bridge-web" \
  && ok "unquarantine: signature re-verified, back in place, restarted" || no "unquarantine failed: $(cat "$T/out")"
unset BRIDGE_OVR_NOW_CMD

# ===================== 10. lifeline guard =====================
printf '#!/usr/bin/env python3\nprint("agent v2")\n' > "$T/agent.py"; publish bridge-agent.py "$T/agent.py"; deploy bridge-agent.py
echo 5000.00 > "$T/uptime"
touch "$T/agent-ok"; bash "$OVR" health >/dev/null 2>&1
[ -e "$D/bridge-agent.py" ] && ok "agent update + fleet heard from recently: kept" || no "agent update reverted while the fleet was reachable"
touch -t 202001010000 "$T/agent-ok" "$D/bridge-agent.py"; bash "$OVR" health >/dev/null 2>&1
[ ! -e "$D/bridge-agent.py" ] && ok "agent update + fleet silent 15 min: reverted automatically (lifeline guard)" || no "lifeline guard did not revert the agent"
touch "$T/agent-ok"

# ===================== 11. boot: apply-all, known-good, safe mode =====================
: > "$T/mountinfo"; echo 0 > "$D/.boot-attempts"
printf '#!/bin/bash\necho TAMPERED\n' > "$D/bridge-status.sh"; cp "$T/server/bridge-web.py.sig" "$D/bridge-status.sh.sig" 2>/dev/null
bash "$OVR" apply-all >/dev/null 2>&1
bound "$T/root/usr/local/bin/bridge-web.py" && bound "$T/root/home/pi/uvc-raw-setup.sh" && ok "boot: every verified override put in place" || no "boot did not bind overrides"
! bound "$T/root/usr/local/bin/bridge-status.sh" && ok "boot: an unverified file is NOT put in place" || no "unverified override bound at boot"
[ "$(cat "$D/.boot-attempts")" = 1 ] && [ ! -d "$D/.pending" ] && ok "boot counter 1, nothing pending after a fresh boot" || no "boot bookkeeping wrong"
echo 200.00 > "$T/uptime"; bash "$OVR" health >/dev/null 2>&1
[ "$(cat "$D/.boot-attempts")" = 0 ] && grep -q '^bridge-web.py ' "$D/.known-good" && ok "healthy boot: counter reset, in-effect files recorded as known good" || no "healthy mark wrong"
rm -f "$D/bridge-status.sh" "$D/bridge-status.sh.sig"
printf '#!/bin/bash\necho NEW-SENTRY\n' > "$T/sentry2.sh"; publish jitter-sentry.sh "$T/sentry2.sh"; deploy jitter-sentry.sh   # not yet proven by a boot
echo 3 > "$D/.boot-attempts"; : > "$T/mountinfo"; rm -f "$T/run/safe-mode"
bash "$OVR" apply-all >/dev/null 2>&1
[ -e "$T/run/safe-mode" ] && [ ! -s "$T/mountinfo" ] && ok "3 unhealthy boots in a row: SAFE MODE, no override applied" || no "safe mode not entered"
mkdir -p "$T/lb" "$T/lo/.state"; printf '#!/bin/bash\necho BAKED\n' > "$T/lb/demo.sh"; chmod +x "$T/lb/demo.sh"
printf '#!/bin/bash\necho OVERRIDE\n' > "$T/lo/demo.sh"; chmod +x "$T/lo/demo.sh"
openssl dgst -sha256 -sign "$T/key.pem" -out "$T/lo/demo.sh.sig" "$T/lo/demo.sh" 2>/dev/null
sed -e "s#^BAKED=.*#BAKED=\"$T/lb/\$NAME\"#" -e "s#^DIR=.*#DIR=\"$T/lo\"#" -e "s#^PUBKEY=\"/etc.*#PUBKEY=\"$T/pub.pem\"#" \
    -e "s#^STATE=.*#STATE=\"$T/lo/.state\"#" "$REPO/pi/scripts/bridge-run.sh" > "$T/run.sh"
[ "$(BRIDGE_RUN_SAFE_FLAG="$T/run/safe-mode" bash "$T/run.sh" demo.sh 2>/dev/null)" = BAKED ] && ok "safe mode: the media loader runs the built-in too" || no "loader ignored safe mode"
[ "$(BRIDGE_RUN_SAFE_FLAG="$T/nope" bash "$T/run.sh" demo.sh 2>/dev/null)" = OVERRIDE ] && ok "outside safe mode the loader still runs a signed override" || no "loader broke"
bash "$OVR" health >/dev/null 2>&1          # uptime 200, core units active -> healthy safe-mode boot
if [ -e "$D/bridge-web.py" ] && [ ! -e "$D/jitter-sentry.sh" ] && [ "$(cat "$D/.boot-attempts")" = 0 ]; then
  ok "safe-mode boot turns healthy: the unproven update is quarantined, the known-good ones stay"
else no "safe-mode resolution wrong (web=$( [ -e "$D/bridge-web.py" ] && echo kept || echo gone ), sentry=$( [ -e "$D/jitter-sentry.sh" ] && echo kept || echo gone ))"; fi

# ===================== 12. status for the fleet =====================
rm -f "$T/run/safe-mode"
out="$(bash "$DEPLOY" --running)"
echo "$out" | head -4 | grep -c -E '^bridge-(return-audio|feeder-audio|feeder-net|uvcd)\.sh ' | grep -qx 4 \
  && echo "$out" | grep -q '^bridge-web.py override sha256=' && ok "'running' = the 4 media lines + every other overridden file" || no "running output: $out"
[ "$(echo "$out" | wc -c)" -lt 2000 ] && ok "'running' fits the fleet's 2000-character output" || no "running output too long for the fleet"

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
