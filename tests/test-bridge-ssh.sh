#!/bin/bash
# Owner SSH (bridge-ssh.sh): tailnet address only, owner key only, no root, no passwords, no
# forwarding. Runs the real script in its test mode (BRIDGE_SSH_ONCE: write + `sshd -t`, exit),
# with this machine's real sshd validating the generated config.
set -uo pipefail
file_mode(){ python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }
HERE="$(cd "$(dirname "$0")" && pwd)"
S="$HERE/../pi/scripts/bridge-ssh.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); }
SSHD="$(command -v sshd || echo /usr/sbin/sshd)"
[ -x "$SSHD" ] || { echo "  SKIP  no sshd on this machine"; exit 0; }

ssh-keygen -q -t ed25519 -N "" -f "$T/owner"
printf 'restrict,pty %s\n' "$(cat "$T/owner.pub")" > "$T/keys"
run(){  # run <tailscale-ip-output>
  printf '#!/bin/bash\necho "%s"\n' "$1" > "$T/tsip"; chmod +x "$T/tsip"
  BRIDGE_SSH_KEYS="$T/keys" BRIDGE_SSH_HOSTKEY_DIR="$T/hk" BRIDGE_SSH_RUNDIR="$T/run" \
  BRIDGE_SSH_PRIVSEP="$T/privsep" BRIDGE_SSH_SSHD="$SSHD" BRIDGE_SSH_TSIP_CMD="$T/tsip" \
  BRIDGE_SSH_ONCE=1 bash "$S" 2>"$T/err"; echo $?
}

rc="$(run 100.67.196.104)"
[ "$rc" = 0 ] && ok "tailnet address: config written and accepted by sshd -t" || no "rc=$rc $(cat "$T/err")"
C="$T/run/sshd_config"
grep -qx 'ListenAddress 100.67.196.104' "$C" && ok "listens on the tailnet address ONLY" || no "ListenAddress: $(grep ListenAddress "$C")"
[ "$(grep -c '^ListenAddress' "$C")" = 1 ] && ok "exactly one ListenAddress (no 0.0.0.0 / LAN)" || no "more than one ListenAddress"
for want in 'PermitRootLogin no' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
            'AuthenticationMethods publickey' 'AllowUsers pi@100.64.0.0/10' 'AllowTcpForwarding no' \
            'AllowAgentForwarding no' 'X11Forwarding no' 'PermitTunnel no' "AuthorizedKeysFile $T/keys"; do
  grep -qx "$want" "$C" && ok "$want" || no "missing: $want"
done
[ -s "$T/hk/ssh_host_ed25519_key" ] && [ "$(file_mode "$T/hk")" = 700 ] && ok "host key created on /data (dir 0700): stable fingerprint across reboots/updates" || no "host key missing"
fp1="$(ssh-keygen -l -f "$T/hk/ssh_host_ed25519_key.pub")"; run 100.67.196.104 >/dev/null
[ "$(ssh-keygen -l -f "$T/hk/ssh_host_ed25519_key.pub")" = "$fp1" ] && ok "host key reused, not regenerated" || no "host key changed on restart"

rc="$(run 192.168.29.109)"
[ "$rc" = 3 ] && ok "LAN address offered as the 'tailnet' address: refused, nothing listens" || no "LAN address accepted (rc $rc)"
rc="$(run 100.128.0.1)"
[ "$rc" = 3 ] && ok "address just outside 100.64.0.0/10: refused" || no "out-of-range address accepted (rc $rc)"
rc="$(run '')"
[ "$rc" = 3 ] && ok "tailnet not up yet: waits, never falls back to all interfaces" || no "no-tailnet case rc $rc"
touch "$T/policy"
export BRIDGE_SIGNATURE_POLICY="$T/policy"
export BRIDGE_SIGNATURE_VERIFIER="$HERE/../pi/scripts/bridge-verify-update.py"
export BRIDGE_SSH_FLOORS="$T/floors"
rc="$(run 100.67.196.104)"
[ "$rc" = 0 ] && ok "strict owner-key command accepted by real sshd" || no "strict config rc=$rc $(cat "$T/err")"
grep -qx 'AuthorizedKeysFile none' "$C" && ok "strict SSH cannot bypass revocation via baked key file" || no "static key fallback enabled"
grep -qx 'AuthorizedKeysCommandUser root' "$C" && grep -q '^AuthorizedKeysCommand /usr/bin/python3 ' "$C" && ok "strict SSH consults accepted signed owner keys on authentication" || no "owner verifier absent"
unset BRIDGE_SIGNATURE_POLICY BRIDGE_SIGNATURE_VERIFIER BRIDGE_SSH_FLOORS
: > "$T/keys"; rc="$(run 100.67.196.104)"
[ "$rc" = 2 ] && ok "no owner key: SSH stays off" || no "empty key file rc $rc"
grep -qE '^restrict,pty ssh-ed25519 ' "$HERE/../pi/configs/owner_ssh_authorized_keys" \
  && ok "the image's owner key is restricted to a shell (restrict,pty)" || no "image owner key missing or unrestricted"

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
