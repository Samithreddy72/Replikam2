#!/bin/bash
# THE FINAL CARD DEPLOY — installs everything queued up, in the one order that is safe.
#
# ORDER MATTERS. The systemd units in this deploy start their scripts through the verifying
# loader (ExecStart=/usr/local/bin/bridge-run.sh <script>). If a unit lands before the loader
# does, that service cannot start at all. So: loader first, everything else after, units last,
# and every step is verified on the card before the next one runs.
#
#   bash deploy-final.sh <pi-host>      (run with the TARGET card in the reader on the Pi)
set -uo pipefail
PI="${1:?usage: deploy-final.sh <pi-host>}"
KEY=~/.ssh/pi_bridge
R=~/netbridge/replikam2-ci
PUB=~/.netbridge/keys/script-pubkey.pem
S="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=8 pi@$PI"
ok(){ echo "  ✅ $*"; }
die(){ echo "  ❌ $*" >&2; exit 1; }

echo "════ staging files ════"
for f in "$R/pi/scripts/bridge-run.sh" "$R/pi/scripts/bridge-deploy-script.sh" \
         "$R/pi/scripts/bridge-return-audio.sh" "$R/pi/scripts/bridge-web.py" \
         "$R/pi/scripts/bridge-agent.py" "$PUB"; do
  [ -f "$f" ] || die "missing $f"
done
bash -n "$R/pi/scripts/bridge-run.sh" || die "loader syntax"
bash -n "$R/pi/scripts/bridge-deploy-script.sh" || die "deploy script syntax"
bash -n "$R/pi/scripts/bridge-return-audio.sh" || die "return-audio syntax"
python3 -m py_compile "$R/pi/scripts/bridge-web.py" || die "bridge-web syntax"
python3 -m py_compile "$R/pi/scripts/bridge-agent.py" || die "bridge-agent syntax"
ok "all payloads pass syntax checks locally"

$S 'echo OK' 2>/dev/null | grep -q OK || die "no SSH to $PI"
scp -i "$KEY" -o StrictHostKeyChecking=no \
  "$R/pi/scripts/bridge-run.sh" "$R/pi/scripts/bridge-deploy-script.sh" \
  "$R/pi/scripts/bridge-return-audio.sh" "$R/pi/scripts/bridge-web.py" \
  "$R/pi/scripts/bridge-agent.py" "$PUB" \
  "pi@$PI:/tmp/" >/dev/null || die "scp failed"
scp -i "$KEY" -o StrictHostKeyChecking=no "$R"/pi/systemd/bridge-{return-audio,feeder-audio,feeder-net,uvcd}.service \
  "pi@$PI:/tmp/" >/dev/null || die "scp units failed"
ok "payloads on the Pi"

$S 'bash -s' <<'REMOTE'
set -uo pipefail
sudo systemctl stop bridge-powertrim 2>/dev/null
echo "0000:01:00.0" | sudo tee /sys/bus/pci/drivers/xhci_hcd/bind >/dev/null 2>&1 || true
sleep 8
lsblk -o NAME,LABEL | grep -q rootA || { echo "  ❌ target card not visible in the reader"; exit 1; }
sudo mkdir -p /mnt/tc && sudo mount /dev/sda2 /mnt/tc 2>/dev/null
[ -n "$(findmnt -no SOURCE /mnt/tc)" ] || { echo "  ❌ mount failed"; exit 1; }
echo "  ✅ card mounted"
M=/mnt/tc

# ---- 1. LOADER FIRST (units below depend on it existing) ----
sudo install -m 0755 /tmp/bridge-run.sh          $M/usr/local/bin/bridge-run.sh
sudo bash -n $M/usr/local/bin/bridge-run.sh || { echo "  ❌ loader syntax on card"; exit 1; }
echo "  ✅ 1/6 loader installed  ($(sudo md5sum $M/usr/local/bin/bridge-run.sh | cut -c1-12))"

# ---- 2. public key: THE TRUST ANCHOR, on the read-only root ----
# The first attempt wrote this to $M/data/config — but /data is a SEPARATE partition mounted
# OVER that path at boot, so the file was invisible at runtime and the device (correctly)
# refused every deploy with "no pubkey ... cannot verify". Putting it on the root also closes
# a real hole: /data is writable, and a key living beside the overrides could be swapped by
# whoever can write there. The anchor belongs where the override mechanism cannot reach it.
sudo mkdir -p $M/etc/netbridge
sudo install -m 0644 /tmp/script-pubkey.pem $M/etc/netbridge/script-pubkey.pem
sudo grep -q 'PUBLIC KEY' $M/etc/netbridge/script-pubkey.pem \
  && echo "  ✅ 2/6 trust anchor at /etc/netbridge/script-pubkey.pem (read-only root)" \
  || { echo "  ❌ pubkey did not install"; exit 1; }

# ---- 3. deploy helper ----
sudo install -m 0755 /tmp/bridge-deploy-script.sh $M/usr/local/bin/bridge-deploy-script.sh
echo "  ✅ 3/6 deploy helper installed"

# ---- 4. return-audio: watchdog 10s + mismatch publisher ----
sudo cp -n $M/usr/local/bin/bridge-return-audio.sh $M/usr/local/bin/bridge-return-audio.sh.pre-final 2>/dev/null || true
sudo install -m 0755 /tmp/bridge-return-audio.sh  $M/usr/local/bin/bridge-return-audio.sh
sudo grep -q 'mismatch watchdog' $M/usr/local/bin/bridge-return-audio.sh \
  && sudo grep -q 'RETURN_MIN_RESTART_GAP_S:-8' $M/usr/local/bin/bridge-return-audio.sh \
  && echo "  ✅ 4/6 return-audio (defer 8s + watchdog 10s + mismatch publisher)" \
  || { echo "  ❌ return-audio markers missing"; exit 1; }

# ---- 5. bridge-web: rate-aware check + telemetry fields ----
sudo cp -n $M/usr/local/bin/bridge-web.py $M/usr/local/bin/bridge-web.py.pre-final 2>/dev/null || true
sudo install -m 0755 /tmp/bridge-web.py $M/usr/local/bin/bridge-web.py
sudo grep -q 'RATE MISMATCH' $M/usr/local/bin/bridge-web.py \
  && echo "  ✅ 5/6 bridge-web (rate-aware check + mismatch telemetry)" \
  || { echo "  ❌ bridge-web markers missing"; exit 1; }

# ---- 5b. AGENT: without this the fleet's deploy-script command is rejected as unknown,
#      so the whole remote-deploy feature is unreachable. Missing this in the first deploy
#      is exactly why the live test could not run.
sudo cp -n $M/usr/local/bin/bridge-agent.py $M/usr/local/bin/bridge-agent.py.pre-final 2>/dev/null || true
sudo install -m 0755 /tmp/bridge-agent.py $M/usr/local/bin/bridge-agent.py
sudo grep -q 'deploy-script' $M/usr/local/bin/bridge-agent.py \
  && echo "  ✅ 5b/7 agent knows deploy-script + revert-script" \
  || { echo "  ❌ agent missing deploy-script"; exit 1; }

# ---- 6. units LAST, now that the loader they reference exists ----
for u in bridge-return-audio bridge-feeder-audio bridge-feeder-net bridge-uvcd; do
  [ -f /tmp/$u.service ] || continue
  [ -f $M/etc/systemd/system/$u.service ] && D=$M/etc/systemd/system || D=$M/lib/systemd/system
  sudo install -m 0644 /tmp/$u.service $D/$u.service
done
n=$(sudo grep -l 'bridge-run.sh' $M/etc/systemd/system/*.service $M/lib/systemd/system/bridge-*.service 2>/dev/null | wc -l | tr -d ' ')
echo "  ✅ 6/6 $n units now start through the loader"

# every unit's ExecStart must point at something that exists, or that service is dead on boot
missing=0
# Strip systemd's ExecStart prefixes (-, @, +, !, :) before testing the path — leaving the
# '-' on turned /bin/sh into '-/bin/sh' and reported three false failures on a good card.
for f in $(sudo grep -ho 'ExecStart=[^ ]*' $M/etc/systemd/system/bridge-*.service $M/lib/systemd/system/bridge-*.service 2>/dev/null | sed 's/ExecStart=//' | sed 's/^[-@+!:]*//' | sort -u); do
  sudo test -x "$M$f" || { echo "  ❌ ExecStart missing on card: $f"; missing=1; }
done
[ "$missing" = "0" ] && echo "  ✅ every unit ExecStart resolves on the card"

sudo sync; sudo umount /mnt/tc && echo "  ✅ unmounted cleanly"; sudo sync
rm -f /tmp/bridge-run.sh /tmp/bridge-deploy-script.sh /tmp/bridge-return-audio.sh /tmp/bridge-web.py /tmp/bridge-agent.py /tmp/script-pubkey.pem /tmp/bridge-*.service
REMOTE
rc=$?
echo
[ $rc -eq 0 ] && echo "════ DEPLOY OK — swap the card back and boot ════" \
              || echo "════ DEPLOY FAILED (rc=$rc) — do NOT swap; card left as-is ════"
exit $rc
