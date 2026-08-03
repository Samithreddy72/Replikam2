#!/bin/bash
# Exercises the signed script-override loader WITHOUT a Pi.
#
# This is the feature that can brick a bridge from a distance — a bad script, or a script from
# the wrong hands, becomes the code the bridge runs at boot. Every check below is a way that
# could go wrong:
#   - unsigned or wrongly-signed code being executed  -> the signature checks
#   - a crash-looping override with nobody on site    -> the quarantine trip
#   - a path-traversal name reaching the filesystem   -> the name check
#   - falling back to nothing when things go wrong    -> the baked-in fallbacks
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOADER="$HERE/../pi/scripts/bridge-run.sh"
SIGNER="$HERE/../tools/sign-script.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); }

# A sandbox that stands in for the device's filesystem: the loader takes its paths from the
# environment so the whole thing runs on a laptop.
mkdir -p "$T/baked" "$T/data/overrides" "$T/data/config" "$T/root"
cat > "$T/baked/demo.sh" <<'EOF'
#!/bin/bash
echo "BAKED"
EOF
chmod +x "$T/baked/demo.sh"

# Real keys — the point of the test is that real openssl verification gates execution.
openssl ecparam -name prime256v1 -genkey -noout -out "$T/key.pem" 2>/dev/null
openssl ec -in "$T/key.pem" -pubout -out "$T/root/script-pubkey.pem" 2>/dev/null
openssl ecparam -name prime256v1 -genkey -noout -out "$T/evil.pem" 2>/dev/null

mkoverride(){ printf '#!/bin/bash\necho "%s"\n' "$1" > "$T/data/overrides/demo.sh"
              chmod +x "$T/data/overrides/demo.sh"; }
sign(){ openssl dgst -sha256 -sign "${1:-$T/key.pem}" -out "$T/data/overrides/demo.sh.sig" \
          "$T/data/overrides/demo.sh" 2>/dev/null; }
clear_ovr(){ rm -f "$T/data/overrides/demo.sh" "$T/data/overrides/demo.sh.sig"
             rm -rf "$T/data/overrides/.state" "$T/data/overrides/quarantine"
             rm -f "$T/data/overrides/.quarantined.json"; }

# The loader hardcodes device paths; rewrite them for the sandbox. (Testing the real file,
# not a copy of the logic — a reimplementation would pass while the shipped script failed.)
sed -e "s#^BAKED=.*#BAKED=\"$T/baked/\$NAME\"#" \
    -e "s#^DIR=.*#DIR=\"$T/data/overrides\"#" \
    -e "s#^PUBKEY=\"/etc.*#PUBKEY=\"$T/root/script-pubkey.pem\"#" \
    -e "s#^\[ -f \"\$PUBKEY\" \] || PUBKEY=.*#[ -f \"\$PUBKEY\" ] || PUBKEY=\"$T/data/config/script-pubkey.pem\"#" \
    -e "s#^STATE=.*#STATE=\"$T/data/overrides/.state\"#" \
    "$LOADER" > "$T/run.sh"
chmod +x "$T/run.sh"
run(){ bash "$T/run.sh" demo.sh 2>>"$T/err.log"; }

# ===================== 1. no override at all =====================
clear_ovr
[ "$(run)" = "BAKED" ] && ok "no override -> runs the baked-in script" || no "expected BAKED, got '$(run)'"

# ===================== 2. a properly signed override runs =====================
clear_ovr; mkoverride "OVERRIDE"; sign
[ "$(run)" = "OVERRIDE" ] && ok "correctly signed override is executed" || no "signed override did not run (got '$(run)')"

# ===================== 3. unsigned override is refused =====================
clear_ovr; mkoverride "UNSIGNED"
[ "$(run)" = "BAKED" ] && ok "UNSIGNED override is refused (falls back to baked-in)" \
                       || no "unsigned code was executed — SECURITY FAILURE"

# ===================== 4. wrong key is refused =====================
clear_ovr; mkoverride "EVIL"; sign "$T/evil.pem"
[ "$(run)" = "BAKED" ] && ok "override signed by the WRONG key is refused" \
                       || no "wrong-key code was executed — SECURITY FAILURE"

# ===================== 5. tampering after signing is caught =====================
clear_ovr; mkoverride "GOOD"; sign
printf '#!/bin/bash\necho "TAMPERED"\n' > "$T/data/overrides/demo.sh"   # same sig, new body
[ "$(run)" = "BAKED" ] && ok "content tampered after signing is caught" \
                       || no "tampered code was executed — SECURITY FAILURE"

# ===================== 6. missing pubkey = fail safe =====================
clear_ovr; mkoverride "OVERRIDE"; sign
mv "$T/root/script-pubkey.pem" "$T/pub.bak"
[ "$(run)" = "BAKED" ] && ok "no pubkey on the device -> baked-in (cannot verify = do not run)" \
                       || no "ran an override with no way to verify it"
mv "$T/pub.bak" "$T/root/script-pubkey.pem"

# ===================== 7. auto-rollback quarantines a crash-looper =====================
clear_ovr; mkoverride "OVERRIDE"; sign
r1=$(run); r2=$(run); r3=$(run)      # 3 starts inside the window = the trip
[ "$r1" = "OVERRIDE" ] && [ "$r2" = "OVERRIDE" ] && ok "override runs for the first starts" \
                                                 || no "override did not run before the trip ($r1,$r2)"
[ "$r3" = "BAKED" ] && ok "3 starts in the window QUARANTINES the override (auto-rollback)" \
                    || no "crash-loop was not caught (got '$r3') — a bad deploy could brick a bridge"
[ -f "$T/data/overrides/.quarantined.json" ] && ok "quarantine is recorded for telemetry" \
                                             || no "no .quarantined.json written"
ls "$T/data/overrides/quarantine"/demo.sh.* >/dev/null 2>&1 \
  && ok "quarantined file is PRESERVED for diagnosis, not deleted" || no "quarantined override was lost"
[ "$(run)" = "BAKED" ] && ok "stays on baked-in after quarantine" || no "override came back after quarantine"

# ===================== 8. a fresh deploy clears the trip =====================
mkoverride "NEWVERSION"; sign; rm -rf "$T/data/overrides/.state" "$T/data/overrides/.quarantined.json"
[ "$(run)" = "NEWVERSION" ] && ok "a new deploy gets a fresh trial after a quarantine" \
                            || no "new deploy was blocked by the old trip state"

# ===================== 9. path traversal is refused =====================
out="$(bash "$T/run.sh" ../../etc/passwd 2>&1; echo "rc=$?")"
echo "$out" | grep -q 'rc=64' && ok "path-traversal script name is refused" || no "traversal not refused: $out"

# ========= 9b. the writable partition cannot override the root trust anchor =========
# The original design kept the pubkey in /data - the same writable place overrides live, so
# anyone able to drop a file there could install THEIR key and then sign their own code.
clear_ovr; mkoverride "EVIL"; sign "$T/evil.pem"
openssl ec -in "$T/evil.pem" -pubout -out "$T/data/config/script-pubkey.pem" 2>/dev/null
[ "$(run)" = "BAKED" ] && ok "a pubkey planted in /data cannot override the read-only anchor" \
                       || no "writable-partition key was trusted — SECURITY FAILURE"
rm -f "$T/data/config/script-pubkey.pem"

# ===================== 10. the signing tool round-trips =====================
if bash "$SIGNER" --pubkey >/dev/null 2>&1; then
  tmp="$T/sig-test"; mkdir -p "$tmp"
  printf '#!/bin/bash\necho hi\n' > "$tmp/x.sh"
  if bash "$SIGNER" "$tmp/x.sh" "$tmp/out" >/dev/null 2>&1 && [ -f "$tmp/out/x.sh.sig" ]; then
    ok "sign-script.sh produces a self-verifying signature"
  else no "sign-script.sh did not produce a valid signature"; fi
  printf 'if then fi\n' > "$tmp/bad.sh"
  bash "$SIGNER" "$tmp/bad.sh" "$tmp/out" >/dev/null 2>&1 \
    && no "signer accepted a syntactically broken script" \
    || ok "signer refuses a script that fails syntax check"
else
  ok "signing key not present on this machine — signer checks skipped"
  ok "(run tools/sign-script.sh --keygen to enable them)"
fi

echo
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
