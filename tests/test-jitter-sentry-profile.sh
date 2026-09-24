#!/bin/bash
# jitter-sentry must never switch a bridge to the LAN profile (owner's standing rule, 2026-09-22),
# and must still move an unset bridge to WAN when the network really is bad. Runs the REAL script
# with stubbed ping/pgrep/ip/logger/sleep and a stub `bridge` that records profile switches.
#   bash tests/test-jitter-sentry-profile.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "  PASS  $1"; }; no(){ fail=$((fail+1)); echo "  FAIL  $1"; }

run(){ # run <net-file-contents|NONE> <good|bad> <loops>  -> prints recorded bridge calls
  local T; T=$(mktemp -d); mkdir "$T/bin"
  printf 'RETURN_DEST_IP=10.0.0.9\nRETURN_DEST_PORT=5004\n' > "$T/peer"
  [ "$1" != NONE ] && printf '%s\n' "$1" > "$T/net"
  if [ "$2" = good ]; then
    printf '#!/bin/bash\necho "8 packets transmitted, 8 received, 0%% packet loss, time 1750ms"\necho "rtt min/avg/max/mdev = 4.1/5.0/6.2/0.5 ms"\n' > "$T/bin/ping"
  else
    printf '#!/bin/bash\necho "8 packets transmitted, 5 received, 37%% packet loss, time 1750ms"\necho "rtt min/avg/max/mdev = 40.1/180.0/400.2/90.5 ms"\n' > "$T/bin/ping"
  fi
  printf '#!/bin/bash\nexit 1\n' > "$T/bin/pgrep"                       # no media flowing
  printf '#!/bin/bash\necho "default via 10.0.0.1 dev wlan0"\n' > "$T/bin/ip"
  printf '#!/bin/bash\necho "$*" >> %s/log\n' "$T" > "$T/bin/logger"
  printf '#!/bin/bash\necho "$*" >> %s/bridge-calls\n' "$T" > "$T/bin/bridge"
  # every loop ends in `sleep 20`; stop the script after N of them
  printf '#!/bin/bash\nn=$(cat %s/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > %s/n\n[ "$n" -gt %d ] && kill -TERM $PPID\nexit 0\n' "$T" "$T" "$3" > "$T/bin/sleep"
  chmod +x "$T"/bin/*
  sed -e "s#/etc/default/bridge-return-audio#$T/peer#g" -e "s#/etc/default/bridge-net#$T/net#g" \
      -e "s#/usr/local/bin/bridge #$T/bin/bridge #g" "$ROOT/pi/scripts/jitter-sentry.sh" > "$T/s.sh"
  PATH="$T/bin:$PATH" timeout 60 bash "$T/s.sh" >/dev/null 2>&1 || true
  cat "$T/bridge-calls" 2>/dev/null; rm -rf "$T"
}
command -v timeout >/dev/null || timeout(){ shift; "$@"; }

WAN=$'# NetBridge media tuning (bridge profile wan)\nNET_VIDEO_LATENCY=300\nNET_AUDIO_LATENCY=300'
calls=$(run "$WAN" good 40)
echo "$calls" | grep -q "profile lan" && no "switched a WAN bridge to LAN after a pristine spell ($calls)" \
  || ok "40 pristine rounds on WAN (old trigger was 30): never switched to LAN"
[ -z "$calls" ] && ok "and restarted nothing at all (no profile command issued)" || no "issued: $calls"

calls=$(run NONE bad 6)
echo "$calls" | grep -q "profile wan" && ok "an unset bridge on a bad network is still moved to WAN" \
  || no "bad network did not trigger WAN (calls: ${calls:-none})"

calls=$(run "$WAN" bad 6)
[ -z "$calls" ] && ok "a bridge already on WAN is left alone on a bad network (no restart)" || no "issued: $calls"

echo; echo "  $pass passed, $fail failed"; [ "$fail" -eq 0 ]
