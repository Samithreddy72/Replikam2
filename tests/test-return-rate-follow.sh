#!/bin/bash
# Exercises bridge-return-audio.sh's rate following WITHOUT a Pi.
#
# This suite exists because the FIRST attempt at rate following passed a read-through and
# then destroyed the audio on real hardware (21.6 dropouts/sec vs 0.2 with no follower).
# Every check below encodes a specific way that version was wrong:
#   - it restarted the pipeline for phantom rate changes  -> "no spurious restart"
#   - it tore down when the host merely PAUSED            -> "rate 0 keeps streaming"
#   - it could cycle without limit                        -> "rate-limit holds"
#   - it trusted whatever it parsed                       -> "implausible rate ignored"
#   - it had no safe behaviour without the kernel control -> "fail-safe to fixed rate"
#
# amixer / alsactl / gst are stubbed so the logic runs on any machine.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../pi/scripts/bridge-return-audio.sh"
T="$(mktemp -d)"
cleanup(){ pkill -P $$ 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); }

mkdir -p "$T/bin"
# --- stub gst: log the rate it was opened with, then behave like a running pipeline ----
cat > "$T/bin/gst" <<'STUB'
#!/bin/bash
for a in "$@"; do
  case "$a" in audio/x-raw,rate=*) echo "${a#audio/x-raw,rate=}" >> "$RATE_LOG"; break;; esac
done
exec sleep 600
STUB
# --- stub amixer: report whatever the test wrote to ratefile --------------------------
cat > "$T/bin/amixer" <<'STUB'
#!/bin/bash
[ -f "$T_HOSTRATE" ] || exit 1          # simulates "control does not exist"
echo "  : values=$(cat "$T_HOSTRATE")"
STUB
# --- stub alsactl monitor: emit an event line whenever the test bumps eventfile --------
cat > "$T/bin/alsactl" <<'STUB'
#!/bin/bash
last=""
while true; do
  cur="$(cat "$T_EVENT" 2>/dev/null)"
  if [ "$cur" != "$last" ]; then echo "card 0 element numid=1,name='Capture Rate'"; last="$cur"; fi
  sleep 0.2
done
STUB
chmod +x "$T/bin"/*
export PATH="$T/bin:$PATH"
export RATE_LOG="$T/rates.log" T_HOSTRATE="$T/hostrate" T_EVENT="$T/event"
: > "$RATE_LOG"

sethost(){ echo "$1" > "$T_HOSTRATE"; }
event(){ date +%s%N > "$T_EVENT"; }
rates(){ tr '\n' ' ' < "$RATE_LOG"; }
nth(){ sed -n "${1}p" "$RATE_LOG"; }
nlines(){ wc -l < "$RATE_LOG" | tr -d ' '; }

RUNSEQ=0
stopall(){ pkill -f "bridge-return-audio.sh" 2>/dev/null; pkill -f "$T/bin/alsactl" 2>/dev/null
           pkill -f 'sleep 600' 2>/dev/null; sleep 0.5; }
run(){ RUNSEQ=$((RUNSEQ+1))
       RETURN_GST="$T/bin/gst" RETURN_AMIXER="$T/bin/amixer" RETURN_ALSACTL="$T/bin/alsactl" \
       RETURN_DEST_IP=10.0.0.1 RETURN_RUNDIR="$T/run$RUNSEQ" \
       RETURN_DEBOUNCE_S=0 RETURN_MIN_RESTART_GAP_S="${1:-0}" \
       bash "$SCRIPT" >> "$T/out.log" 2>&1 & echo $!; }

# ============================ 1. fail-safe, no control ============================
rm -f "$T_HOSTRATE"; : > "$RATE_LOG"
P=$(run 0)
# Poll rather than sleep a fixed 2s: on a loaded machine the message can land later, and a
# flaky test is worse than no test.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  grep -q "no 'Capture Rate' control" "$T/out.log" 2>/dev/null && break; sleep 0.5
done
grep -q "no 'Capture Rate' control" "$T/out.log" && ok "no control -> fail-safe message" \
   || no "no control -> expected fail-safe message"
kill -9 $P 2>/dev/null; stopall

# ============================ 2. follows the host ============================
: > "$RATE_LOG"; : > "$T/out.log"; sethost 48000; event
P=$(run 0); sleep 2
[ "$(nth 1)" = "48000" ] && ok "starts at the host's rate (48000)" || no "expected 48000, got '$(nth 1)'"

sleep 1
[ "$(nlines)" = "1" ] && ok "no spurious restart while the rate is unchanged" \
                      || no "pipeline restarted with no rate change (got: $(rates))"

sethost 44100; event; sleep 2.5
[ "$(nth 2)" = "44100" ] && ok "follows a live switch to 44100" || no "did not follow to 44100 (got: $(rates))"

# host pauses -> control reads 0 -> must NOT tear down
n_before=$(nlines); sethost 0; event; sleep 2
[ "$(nlines)" = "$n_before" ] && ok "rate 0 (host paused) keeps streaming" \
                              || no "pause caused a restart (got: $(rates))"

# implausible value must be ignored
sethost 96000; event; sleep 2
[ "$(nlines)" = "$n_before" ] && ok "implausible rate 96000 ignored" || no "acted on a bad rate (got: $(rates))"

sethost 32000; event; sleep 2.5
[ "$(nth 3)" = "32000" ] && ok "follows a live switch to 32000" || no "did not follow to 32000 (got: $(rates))"
kill -9 $P 2>/dev/null; stopall

# ============================ 3. rate-limit is enforced ============================
# The FIRST change is always allowed (blocking a legitimate switch would be a bug). The
# limit governs a SECOND change inside the window - that is what stops a cascade.
: > "$RATE_LOG"; : > "$T/out.log"; sethost 48000; event
P=$(run 60); sleep 2                      # 60s minimum gap
sethost 44100; event; sleep 2.5           # 1st change: expected to be honoured
[ "$(nth 2)" = "44100" ] && ok "first rate change is honoured despite the limit" \
                         || no "first change was wrongly blocked (got: $(rates))"
sethost 32000; event; sleep 2.5           # 2nd change, well inside 60s: must be blocked
[ "$(nlines)" = "2" ] && ok "rate-limit blocks a too-soon re-open (the 21.6/s safety net)" \
                      || no "rate-limit did not hold (got: $(rates))"
grep -q "ignored (re-opened" "$T/out.log" && ok "rate-limit is logged, not silent" \
                                          || no "rate-limit suppressed without a log line"
kill -9 $P 2>/dev/null; stopall

# ============ 4b. THE WEDGE (2026-07-31 hardware failure, must never recur) ============
# File says 48000, but the DEVICE is at 44100 and no change event will ever arrive (the
# control is steady). A pipeline restart must reconcile to the LIVE control, not loop on
# the stale file — on hardware this crash-looped until a human changed the rate on Windows.
stopall; : > "$RATE_LOG"; : > "$T/out.log"; sethost 48000; event
P=$(run 0); sleep 2
sethost 44100                                   # device moves; NO event (steady control)
pkill -f 'sleep 600' 2>/dev/null; sleep 4       # pipeline dies -> supervisor restarts
last=$(tail -1 "$RATE_LOG")
[ "$last" = "44100" ] && ok "restart reconciles to the LIVE rate (no wedge on stale file)" \
                      || no "restarted on the stale file rate (got: $(rates))"
kill -9 $P 2>/dev/null; stopall

# ============================ 4. crash recovery ============================
: > "$RATE_LOG"; : > "$T/out.log"; sethost 48000; event
P=$(run 0); sleep 2
pkill -f 'sleep 600' 2>/dev/null; sleep 5          # simulate a crash (supervisor backs off 2s first)
[ "$(nlines)" -ge 2 ] && ok "pipeline crash is restarted automatically" \
                      || no "crash was not recovered (got: $(rates))"
kill -9 $P 2>/dev/null; stopall

echo
echo "  rates opened: $(rates)"
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
