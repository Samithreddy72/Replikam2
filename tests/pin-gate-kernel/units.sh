#!/usr/bin/env bash
# bridge-pin's two units under real systemd (2026-09-25): the gate is armed and closed by the boot
# unit, and the session watcher re-arms a gate that someone deletes and comes back if it is killed.
# Runs on a throwaway CI machine: it installs bridge-pin into /usr/local/bin and the units into
# /etc/systemd/system, exactly where the bridge image puts them.
#            sudo bash tests/pin-gate-kernel/units.sh
set -uo pipefail
cd "$(dirname "$0")/../.."
[ "$(id -u)" = 0 ] || { echo "needs root"; exit 2; }
command -v nft >/dev/null || { apt-get update -qq >/dev/null; apt-get install -y -qq nftables >/dev/null; }
pass=0; fail=0
check(){ if eval "$2"; then pass=$((pass+1)); echo "  PASS  $1"; else fail=$((fail+1)); echo "  FAIL  $1"; fi; }

install -m 0755 pi/scripts/bridge-pin /usr/local/bin/bridge-pin
install -m 0644 pi/systemd/bridge-pin-gate.service pi/systemd/bridge-pin-sessions.service /etc/systemd/system/
systemctl daemon-reload
out=$(systemd-analyze verify /etc/systemd/system/bridge-pin-gate.service /etc/systemd/system/bridge-pin-sessions.service 2>&1)
printf '%s\n' "$out" | sed 's/^/    verify: /'
check "systemd-analyze reports nothing wrong with either unit" '! printf "%s\n" "$out" | grep -i "bridge-pin" | grep -qiE "error|invalid|unknown|failed|not executable"'
check "the boot unit enables into sysinit.target" \
  'systemctl enable bridge-pin-gate.service >/dev/null 2>&1 && [ -L /etc/systemd/system/sysinit.target.wants/bridge-pin-gate.service ]'
check "the watcher enables into multi-user.target" \
  'systemctl enable bridge-pin-sessions.service >/dev/null 2>&1 && [ -L /etc/systemd/system/multi-user.target.wants/bridge-pin-sessions.service ]'

systemctl start bridge-pin-gate.service
check "bridge-pin-gate runs to completion (active, exited)" '[ "$(systemctl is-active bridge-pin-gate.service)" = active ]'
check "…it armed the real gate" 'nft list table inet netbridge_gate >/dev/null 2>&1'
check "…closed, as the public state file says" \
  'python3 -c "import json,sys; sys.exit(json.load(open(\"/run/bridge-pin/state.json\"))[\"gate\"] != \"closed\")"'
check "…state file world-readable (0644), no session file" \
  '[ "$(stat -c %a /run/bridge-pin/state.json)" = 644 ] && [ ! -e /run/bridge-pin/session.json ]'

systemctl start bridge-pin-sessions.service
sleep 2
check "the session watcher is running" '[ "$(systemctl is-active bridge-pin-sessions.service)" = active ]'
nft delete table inet netbridge_gate
t0=$(date +%s)
while ! nft list table inet netbridge_gate >/dev/null 2>&1 && [ $(( $(date +%s) - t0 )) -lt 25 ]; do sleep 1; done
check "a deleted gate is re-armed by the watcher (after $(( $(date +%s) - t0 )) s; it sweeps every 15 s)" \
  'nft list table inet netbridge_gate >/dev/null 2>&1'
systemctl kill -s KILL bridge-pin-sessions.service
sleep 8
check "the watcher comes back after being killed (Restart=always, 5 s)" '[ "$(systemctl is-active bridge-pin-sessions.service)" = active ]'
journalctl -t bridge-pin --no-pager -n 5 2>/dev/null | sed 's/^/    journal: /'

systemctl stop bridge-pin-sessions.service bridge-pin-gate.service
echo; echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
