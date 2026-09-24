#!/bin/bash
# The camera service's USB-miss counter must count isochronous misses from the kernel log and
# attribute USB interrupts to CPUs - it is the referee for every video tuning decision since
# 2026-09-24. Runs the REAL bridge-uvcd.sh with a fake /dev/kmsg (FIFO), a fake /proc/interrupts
# and a fake uvc-gadget, injects known messages, and checks the 10-second report.
#   bash tests/test-uvcd-miss-counter.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'pkill -f "$T" 2>/dev/null; rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "  PASS  $1"; }; no(){ fail=$((fail+1)); echo "  FAIL  $1"; }
command -v python3 >/dev/null || { echo "SKIP: python3 missing"; exit 0; }

mkfifo "$T/kmsg"; mkdir "$T/bin"
printf 'CPU0 CPU1 CPU2 CPU3\n 34: 100 0 5000 0 GICv2 105 Level fe980000.usb, fe980000.usb\n' > "$T/interrupts"
printf '#!/bin/bash\nwhile [ "${1#-}" != "$1" ]; do shift; done; exec "$@"\n' > "$T/bin/stdbuf"
printf '#!/bin/bash\nsleep 24\n' > "$T/fake-gadget"; chmod +x "$T/bin/stdbuf" "$T/fake-gadget"
sed -e "s#^export PATH=#export PATH=$T/bin:#" -e "s#/usr/local/bin/uvc-gadget#$T/fake-gadget#" \
    "$ROOT/pi/scripts/bridge-uvcd.sh" > "$T/s.sh"
( exec 3>"$T/kmsg"; sleep 2
  for m in "6,1,1,-;configfs-gadget.g1 gadget.0: uvc: VS request completed with status -61." \
           "6,2,2,-;configfs-gadget.g1 gadget.0: uvc: VS request completed with status -61." \
           "4,3,3,-;usb 1-1: some unrelated message" \
           "6,4,4,-;configfs-gadget.g1 gadget.0: uvc: VS request completed with status -18." \
           "6,5,5,-;configfs-gadget.g1 gadget.0: uvc: VS request completed with status -61."; do
    printf '%s\n' "$m" >&3; sleep 0.3; done
  sleep 3
  printf 'CPU0 CPU1 CPU2 CPU3\n 34: 100 0 9000 0 GICv2 105 Level fe980000.usb, fe980000.usb\n' > "$T/interrupts"
  sleep 20 ) &
KMSG="$T/kmsg" PROC_INT="$T/interrupts" USB_STATS="$T/stats.txt" bash "$T/s.sh" >/dev/null 2>&1
sleep 1
L1=$(sed -n 1p "$T/stats.txt" 2>/dev/null); L2=$(sed -n 2p "$T/stats.txt" 2>/dev/null)
echo "        report: $L1"
[[ "$L1" == *"enodata=3 "* ]] && ok "three -61 misses counted" || no "-61 count wrong ($L1)"
[[ "$L1" == *"exdev=1 "* ]] && ok "one -18 (missed transfer) counted" || no "-18 count wrong"
[[ "$L1" == *"other=0 "* ]] && ok "unrelated kernel messages ignored" || no "unrelated message counted"
[[ "$L1" == *"irq_cpu=0/0/4000/0 "* ]] && ok "USB interrupts attributed to CPU 2" || no "irq attribution wrong"
[[ "$L2" == *"enodata=0 exdev=0 other=0"* ]] && ok "the next window starts from zero" || no "counts leaked into the next window ($L2)"
grep -q 'USB_IRQ_CPUS="2"' "$ROOT/pi/scripts/bridge-uvcd.sh" && ok "default USB interrupt placement is CPU 2" || no "IRQ default changed"
echo; echo "  $pass passed, $fail failed"; [ "$fail" -eq 0 ]
