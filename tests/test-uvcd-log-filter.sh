#!/bin/bash
# bridge-uvcd.sh must drop the EAGAIN flood and keep everything else.
#
# The fake gadget prints on the SAME streams as the real one (libuvcgadget, see
# sources/patched-uvc-gadget-sources.tgz): the dequeue message is printf() -> stdout, the pump
# telemetry is fprintf(stderr). The first version of this filter was tested against a fake
# that printed everything to stderr, passed, and removed nothing on the bridge.
#   bash tests/test-uvcd-log-filter.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "  PASS  $1"; }
no(){ fail=$((fail+1)); echo "  FAIL  $1"; }

mkdir -p "$T/bin"
cat > "$T/bin/stdbuf" <<'S'
#!/bin/bash
while [ "${1#-}" != "$1" ]; do shift; done; exec "$@"
S
cat > "$T/fake-gadget" <<'S'
#!/bin/bash
for i in 1 2 3; do
  echo "/dev/video40: unable to dequeue buffer index 0/2 (11)"
  echo "/dev/video40: unable to dequeue buffer index 1/2 (11)"
done
echo "/dev/video40: unable to dequeue buffer index 0/2 (19)"
echo "Stopping video stream."
echo "pump: ok=40 again=900 err=0(errno=0) gray=0 idle=0 bytesused=460800" >&2
echo "pump: source STREAMOFF" >&2
S
chmod +x "$T/bin/stdbuf" "$T/fake-gadget"
sed -e "s#^export PATH=.*#export PATH=$T/bin:/usr/local/bin:/usr/bin:/bin#" \
    -e "s#/usr/local/bin/uvc-gadget#$T/fake-gadget#" \
  "$ROOT/pi/scripts/bridge-uvcd.sh" > "$T/uvcd.sh"

bash "$T/uvcd.sh" > "$T/out" 2> "$T/err"
sleep 0.5    # the filter is a process substitution: let it drain after the gadget exits

n=$(grep -c "(11)$" "$T/out" "$T/err" | awk -F: '{s+=$2} END{print s}')
[ "$n" -eq 0 ] && ok "EAGAIN (11) lines removed (left: $n)" || no "EAGAIN (11) lines removed (left: $n)"
grep -q "(19)$" "$T/out" && ok "dequeue failure with another errno kept" || no "dequeue failure with another errno kept"
grep -q "Stopping video stream." "$T/out" && ok "other stdout lines kept" || no "other stdout lines kept"
grep -q "pump: ok=40" "$T/err" && ok "pump telemetry kept on stderr" || no "pump telemetry kept on stderr"
grep -q "STREAMOFF" "$T/err" && ok "other stderr lines kept" || no "other stderr lines kept"

echo; echo "  $pass passed, $fail failed"; [ "$fail" -eq 0 ]
