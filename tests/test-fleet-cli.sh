#!/usr/bin/env bash
# The legacy repo-root `fleet` CLI never reports a refused request as success (2026-09-28).
#
# Its api() used plain `curl -s`, so a 4xx body was printed and the caller's `&& echo ✓` still ran:
# once the fleet started refusing a second claim (409), `fleet provision` printed "provision staged"
# although nothing was staged. A stand-in curl on PATH plays the fleet; nothing leaves this machine.
#   bash tests/test-fleet-cli.sh
set -uo pipefail
cd "$(dirname "$0")/.."
TOP=$(pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/home" "$T/bin"
printf 'FLEET_URL=http://fleet.test\nFLEET_KEY=admin-key\n' > "$T/home/.fleetrc"
cat > "$T/bin/curl" <<'SH'
#!/usr/bin/env bash
# stand-in fleet: logs argv + stdin, answers like the real API; like real curl it adds the status only when asked (-w)
log="$FAKE_DIR/calls.log"; printf 'ARGV:' >> "$log"; printf ' [%s]' "$@" >> "$log"; echo >> "$log"
stdin=""; w=""; for a in "$@"; do [ "$a" = "@-" ] && stdin=$(cat); [ "$a" = "-w" ] && w=1; done
reply(){ if [ -n "$w" ]; then printf '%s\n%s' "$1" "$2"; else printf '%s' "$1"; fi; }
[ -n "$stdin" ] && printf 'STDIN: %s\n' "$stdin" >> "$log"
url="${@: -1}"
[ -n "${FAKE_DOWN:-}" ] && { [ -n "$w" ] && printf '\n000'; exit 7; }
case "$url" in
  */admin/devices) reply '[{"id":"dev1","name":"Hall","hostname":"nb-aaaa","pairing_code":"BRIDGE-AAAA"}]' 200 ;;
  */admin/devices/dev1) reply '{"id":"dev1","name":"Hall"}' 200 ;;
  */admin/devices/dev1/mesh-key) reply "{\"detail\":\"${FAKE_MESH_DETAIL:-staged}\"}" "${FAKE_MESH_CODE:-200}" ;;
  */admin/devices/dev1/claim) reply '{"detail":"this bridge is already claimed"}' 409 ;;
  */admin/devices/dev1/commands) reply '{"detail":"unknown command: unlock"}' 400 ;;
  *) reply '{"detail":"not found"}' 404 ;;
esac
SH
chmod +x "$T/bin/curl"
passed=0; failed=0
check(){ if eval "$2"; then passed=$((passed+1)); echo "  PASS  $1"; else failed=$((failed+1)); echo "  FAIL  $1"; fi; }
run(){  # run [script] args... -> $OUT (stdout+stderr), $RC
  local script="$1"; shift
  : > "$T/calls.log"
  OUT=$(HOME="$T/home" PATH="$T/bin:$PATH" FAKE_DIR="$T" bash "$script" "$@" 2>&1); RC=$?
}
KEY="tskey-auth-SECRET123"

echo; echo "fleet provision"; echo "==============="
run fleet provision Hall "tailscale_auth_key=$KEY"
check "stages the key through the re-key endpoint and says so" '[ $RC -eq 0 ] && grep -q "/admin/devices/dev1/mesh-key" "$T/calls.log" && [[ "$OUT" == *"mesh key staged"* ]]'
check "the key travels on stdin" 'grep -q "STDIN: .*$KEY" "$T/calls.log"'
check "the key is never on curl's command line (ps would show it)" '! grep "^ARGV:" "$T/calls.log" | grep -q "$KEY"'
check "no second claim is attempted" '! grep -q "/claim" "$T/calls.log"'
run fleet provision Hall wifi_psk=hunter2
check "anything but the mesh key is refused before sending (exit 1, nothing sent to the endpoint)" \
  '[ $RC -eq 1 ] && ! grep -q "mesh-key" "$T/calls.log" && [[ "$OUT" == *"only tailscale_auth_key"* ]]'
FAKE_MESH_CODE=409 FAKE_MESH_DETAIL="this bridge is already claimed" run fleet provision Hall "tailscale_auth_key=$KEY"
check "a refusal (409) fails loudly with the fleet's reason and never prints a tick" \
  '[ $RC -ne 0 ] && [[ "$OUT" == *"refused this (409)"* ]] && [[ "$OUT" == *"already claimed"* ]] && [[ "$OUT" != *"✓"* ]]'

echo; echo "every command, not just provision"; echo "================================="
run fleet unlock 1234 Hall
check "a command the fleet rejects (400) is an error, not a silent success" '[ $RC -ne 0 ] && [[ "$OUT" == *"refused this (400)"* ]]'
FAKE_DOWN=1 run fleet status
check "an unreachable fleet says so and exits 2" '[ $RC -eq 2 ] && [[ "$OUT" == *"unreachable"* ]]'

echo; echo "the test has teeth: the old CLI (base 7401809)"; echo "============================================="
git show 7401809:fleet > "$T/old-fleet"
FAKE_MESH_CODE=409 run "$T/old-fleet" provision Hall "tailscale_auth_key=$KEY"
check "the old CLI re-claimed (a path the fleet now refuses) AND still printed its tick: the false success this test catches" \
  'grep -q "/claim" "$T/calls.log" && [[ "$OUT" == *"provision staged"* ]]'

echo; echo "  $passed passed, $failed failed"
[ "$failed" -eq 0 ]
