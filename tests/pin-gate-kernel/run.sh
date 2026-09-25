#!/usr/bin/env bash
# The PIN media gate on a REAL Linux kernel (2026-09-25).
#
# bridge-pin's gate is an nftables table. Every other test drives it through a fake nft, so until
# this test the ruleset had never been loaded by a kernel. Here the real tool runs against the real
# kernel in network namespaces: "br" is the bridge, "lan" holds the presenter (.2 / ::2) and someone
# else on the mesh (.3 / ::3), all on tailnet addresses as bridge-pin requires. Every "DELIVERED"
# or "DROPPED" below is a datagram that did or did not reach a socket listening on the bridge.
#
# Needs root and Linux (namespaces, nftables, tmpfs). CI runs it on GitHub's ubuntu runner twice:
# with Debian trixie's userland (what the bridge image ships: nft 1.1, Python 3.13) and with the
# runner's own.            sudo bash tests/pin-gate-kernel/run.sh
set -uo pipefail
cd "$(dirname "$0")/../.."
TOP=$(pwd)
PINTOOL=$TOP/pi/scripts/bridge-pin
UDP=$TOP/tests/pin-gate-kernel/udp.py
[ "$(id -u)" = 0 ] || { echo "needs root"; exit 2; }
if ! command -v nft >/dev/null || ! command -v ip >/dev/null || ! command -v python3 >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y -qq nftables iproute2 python3 >/dev/null
fi
NFT=$(command -v nft)
echo "kernel $(uname -r) | $($NFT --version) | $(python3 --version)"

W=$(mktemp -d); ETC=$W/etc; RUNDIR=$W/run; mkdir -p "$ETC" "$RUNDIR"; RECV=""
cleanup(){
  [ -n "$RECV" ] && kill "$RECV" 2>/dev/null
  ip netns del br 2>/dev/null; ip netns del lan 2>/dev/null
  umount "$W/full" 2>/dev/null; rm -rf "$W"
}
trap cleanup EXIT
pass=0; fail=0
check(){ if eval "$2"; then pass=$((pass+1)); echo "  PASS  $1"; else fail=$((fail+1)); echo "  FAIL  $1"; fi; }
sec(){ echo; echo "$1"; }

# ------------------------------------------------------------------ network
ip netns add br && ip netns add lan || { echo "cannot create network namespaces"; exit 2; }
v6on(){      # docker disables IPv6 in containers by default; the IPv6 cases must not fail for that
  ip netns exec "$1" sh -c "for f in /proc/sys/net/ipv6/conf/all/disable_ipv6 /proc/sys/net/ipv6/conf/default/disable_ipv6 $2; do [ -e \$f ] && echo 0 > \$f; done"
}
v6on br ""; v6on lan ""
ip link add vb type veth peer name vl
ip link set vb netns br
ip link set vl netns lan
v6on br /proc/sys/net/ipv6/conf/vb/disable_ipv6; v6on lan /proc/sys/net/ipv6/conf/vl/disable_ipv6
ip -n br addr add 100.99.0.1/24 dev vb
ip -n br addr add fd7a:115c:a1e0::1/64 dev vb nodad
for a in 100.99.0.2/24 100.99.0.3/24; do ip -n lan addr add "$a" dev vl; done
for a in fd7a:115c:a1e0::2/64 fd7a:115c:a1e0::3/64; do ip -n lan addr add "$a" dev vl nodad; done
for ns in br lan; do ip -n "$ns" link set lo up; done
ip -n br link set vb up
ip -n lan link set vl up
sleep 1

ip netns exec br python3 -u "$UDP" recv > "$W/recv.log" 2>&1 &
RECV=$!
for _ in $(seq 1 50); do grep -q ready "$W/recv.log" && break; sleep 0.1; done
grep -q ready "$W/recv.log" || { echo "receiver did not start"; cat "$W/recv.log"; exit 2; }

N=0
send(){ N=$((N + 1)); TAG="t$N"; ip netns exec lan python3 "$UDP" send "$1" "$2" "$3" "$TAG"; }
delivered(){  # SRC DST PORT: the datagram reached the bridge, from SRC, on PORT
  send "$1" "$2" "$3"
  for _ in $(seq 1 20); do grep -qx "$1 $3 $TAG" "$W/recv.log" && return 0; sleep 0.05; done
  return 1
}
dropped(){    # SRC DST PORT: nothing with this datagram's tag arrived
  send "$1" "$2" "$3"; sleep 0.4
  ! grep -q " $TAG\$" "$W/recv.log"
}
V4=100.99.0.1; V6=fd7a:115c:a1e0::1; P4=100.99.0.2; O4=100.99.0.3; P6=fd7a:115c:a1e0::2; O6=fd7a:115c:a1e0::3

bp(){ ip netns exec br env BRIDGE_PIN_ETC="$ETC" BRIDGE_PIN_RUN="$RUNDIR" BRIDGE_PIN_NFT="$NFT" \
        BRIDGE_PIN_IDLE_S="${IDLE:-600}" python3 "$PINTOOL" "$@"; }
unlock(){ printf '%s\n' "$1" | bp unlock - --peer "$2"; }
field(){ python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1], ""))' "$1"; }
st(){ bp state | python3 -c 'import json,sys; d=json.load(sys.stdin); k=sys.argv[1]
print({"gate": d.get("gate"), "active": d["session"]["active"], "last": d.get("last_end", {}).get("reason")}[k])' "$1"; }
counter(){ ip netns exec br "$NFT" list counter inet netbridge_gate "$1" 2>/dev/null | awk '/packets/{print $2}'; }

# ------------------------------------------------------------------ tests
sec "Before the gate exists"
check "harness: a datagram reaches the bridge's socket" 'delivered $P4 $V4 5000'

sec "Boot: gate-init closes the media ports"
echo "    $(bp gate-init)"
check "gate-init armed a real nftables table (the kernel accepted the ruleset)" \
  'ip netns exec br "$NFT" list table inet netbridge_gate >/dev/null'
check "state says closed" '[ "$(st gate)" = closed ]'
check "video (5000) from the presenter DROPPED" 'dropped $P4 $V4 5000'
check "voice (5002) from the presenter DROPPED" 'dropped $P4 $V4 5002'
check "IPv6 video DROPPED" 'dropped $P6 $V6 5000'
check "other ports untouched: 5004 from anyone DELIVERED" 'delivered $O4 $V4 5004'
check "the refused counter counted the drops" 'c=$(counter refused_in); [ "${c:-0}" -ge 3 ]'

sec "PIN and session"
check "no PIN set: unlock refused (exit 5)" 'unlock 4321 $P4 >/dev/null; [ $? -eq 5 ]'
check "set a PIN (read from stdin)" 'printf "4321\n" | bp set - >/dev/null'
check "a wrong PIN refused (exit 1)" 'unlock 1111 $P4 >/dev/null; [ $? -eq 1 ]'
check "…and the gate stays closed" 'dropped $P4 $V4 5000'
J=$(unlock 4321 $P4); RC=$?
TICKET=$(printf '%s' "$J" | field ticket)
check "the right PIN opens a session: exit 0, a 256-bit ticket, gate open" \
  '[ $RC -eq 0 ] && [ ${#TICKET} -eq 64 ] && [ "$(printf "%s" "$J" | field gate)" = open ]'
check "video from the presenter DELIVERED" 'delivered $P4 $V4 5000'
check "voice from the presenter DELIVERED" 'delivered $P4 $V4 5002'
check "video from anyone else DROPPED" 'dropped $O4 $V4 5000'
check "voice from anyone else DROPPED" 'dropped $O4 $V4 5002'
check "an IPv4 session does not admit the presenter's IPv6 address" 'dropped $P6 $V6 5000'
check "the video counter counts only the presenter's video" '[ "$(counter video_in)" -ge 1 ]'
bp sweep >/dev/null
check "the kernel admits exactly the presenter (sweep reads the table back: open)" '[ "$(st gate)" = open ]'
check "the ticket checks out from the presenter's address" 'printf "%s\n" "$TICKET" | bp check - --peer $P4 >/dev/null'
check "…but not from another address without --rebind (exit 13)" \
  'printf "%s\n" "$TICKET" | bp check - --peer $O4 >/dev/null; [ $? -eq 13 ]'
check "a forged ticket is refused (exit 11)" 'printf "%064d\n" 0 | bp check - --peer $P4 >/dev/null; [ $? -eq 11 ]'
check "with --rebind the session follows its ticket to a new address" \
  'printf "%s\n" "$TICKET" | bp check - --peer $O4 --rebind >/dev/null'
check "…the gate now admits the new address" 'delivered $O4 $V4 5000'
check "…and drops the old one" 'dropped $P4 $V4 5000'
check "Stop (end with the ticket) ends the session" 'printf "%s\n" "$TICKET" | bp end - | grep -q "\"ended\": true"'
check "…and closes the gate" 'dropped $O4 $V4 5000'

sec "IPv6 presenter"
TICKET6=$(unlock 4321 $P6 | field ticket)
check "an IPv6 session opens" '[ ${#TICKET6} -eq 64 ]'
check "IPv6 video from the presenter DELIVERED" 'delivered $P6 $V6 5000'
check "IPv6 video from anyone else DROPPED" 'dropped $O6 $V6 5000'
check "IPv4 from the previous presenter DROPPED" 'dropped $P4 $V4 5000'

sec "Someone flushes the firewall"
ip netns exec br "$NFT" flush ruleset
check "with no rules at all, anyone gets through (the risk the watcher closes)" 'delivered $O6 $V6 5000'
bp sweep >/dev/null
check "one sweep re-arms the gate for the live session" '[ "$(st gate)" = open ]'
check "…the presenter still gets through" 'delivered $P6 $V6 5000'
check "…and anyone else is dropped again" 'dropped $O6 $V6 5000'

sec "Admin lock, PIN change, lockout"
check "an admin lock ends the live session" 'bp lock | grep -q "the live session was ended"'
check "…and closes the gate" 'dropped $P6 $V6 5000'
unlock 4321 $P4 >/dev/null
check "setting a new PIN ends a live session" 'printf "9876\n" | bp set - | grep -q "the live session was ended"'
check "…and closes the gate" 'dropped $P4 $V4 5000'
check "the old PIN no longer works (wrong try 1 of 3)" 'unlock 4321 $P4 >/dev/null; [ $? -eq 1 ]'
check "wrong try 2 of 3 (exit 1)" 'unlock 1111 $P4 >/dev/null; [ $? -eq 1 ]'
check "wrong try 3 of 3 locks the bridge (exit 2)" 'unlock 2222 $P4 >/dev/null; [ $? -eq 2 ]'
check "locked out: even the right PIN is refused (exit 3)" 'unlock 9876 $P4 >/dev/null; [ $? -eq 3 ]'
check "…and nothing gets through" 'dropped $P4 $V4 5000'
bp clear-lockout >/dev/null
check "after an admin clears the lockout the right PIN works" 'unlock 9876 $P4 >/dev/null'

sec "Idle relock (limit shortened to 3 s for the test)"
for _ in 1 2 3 4 5; do send $P4 $V4 5000; sleep 1; IDLE=3 bp sweep >/dev/null; done
check "with video flowing, 5 s of sweeps keep the session open" '[ "$(st active)" = True ]'
sleep 4; IDLE=3 bp sweep >/dev/null
check "after 3 s without video the next sweep relocks it (reason: idle)" '[ "$(st active)" = False ] && [ "$(st last)" = idle ]'
check "…and the gate is closed" 'dropped $P4 $V4 5000'

sec "Reboot"
unlock 9876 $P4 >/dev/null
bp gate-init >/dev/null
check "gate-init (what boot runs) forgets the session" '[ "$(st active)" = False ]'
check "…and the gate is closed" 'dropped $P4 $V4 5000'

sec "Storage full: a PIN try that cannot be counted is refused"
mkdir -p "$W/full" && mount -t tmpfs -o size=8k tmpfs "$W/full"
cp "$ETC/pin.hash" "$W/full/"
dd if=/dev/zero of="$W/full/filler" bs=1k count=64 2>/dev/null
full_unlock(){ printf '%s\n' "$1" | ip netns exec br env BRIDGE_PIN_ETC="$W/full" BRIDGE_PIN_RUN="$RUNDIR" \
                 BRIDGE_PIN_NFT="$NFT" python3 "$PINTOOL" unlock - --peer "$2"; }
check "even the RIGHT PIN is refused (exit 4): no uncounted guesses" 'full_unlock 9876 $P4 >/dev/null; [ $? -eq 4 ]'
check "…and nothing gets through" 'dropped $P4 $V4 5000'

echo; echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
