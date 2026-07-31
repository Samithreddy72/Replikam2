#!/bin/bash
# Exercises bridge-return-audio.sh's phase-6 rate following WITHOUT a Pi.
#
# Worth having off-hardware because the failure modes are all timing/state ones that a
# read-through does not catch: following a rate that never changed (pipeline thrash), tearing
# down when the client merely PAUSES (return audio dies mid-meeting), or re-opening hw:
# before the old handle is released (-EBUSY, looks like "it just stopped working").
#
# gst-launch-1.0 is stubbed with a script that records the rate it was asked for and then
# sleeps, so we can assert on what the pipeline was actually started with.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../pi/scripts/bridge-return-audio.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"; kill %1 2>/dev/null' EXIT

pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; fail=$((fail+1)); }

# --- stub gst-launch-1.0: log the negotiated rate, then act like a running pipeline -------
mkdir -p "$T/bin"
cat > "$T/bin/gst-launch-1.0" <<'STUB'
#!/bin/bash
for a in "$@"; do
  case "$a" in audio/x-raw,rate=*) echo "${a#audio/x-raw,rate=}" >> "$GST_RATE_LOG"; break;; esac
done
exec sleep 300
STUB
chmod +x "$T/bin/gst-launch-1.0"
export GST_RATE_LOG="$T/rates.log"; : > "$GST_RATE_LOG"

# --- fake ALSA ----------------------------------------------------------------------------
set_rate(){   # set_rate <hz>|closed
  mkdir -p "$T/asound/card0/pcm0c/sub0"
  if [ "$1" = "closed" ]; then echo closed > "$T/asound/card0/pcm0c/sub0/hw_params"
  else printf 'access: MMAP_INTERLEAVED\nformat: S16_LE\nchannels: 2\nrate: %s (%s/1)\n' "$1" "$1" \
        > "$T/asound/card0/pcm0c/sub0/hw_params"; fi
}
rates(){ tr '\n' ' ' < "$GST_RATE_LOG"; }
nth(){ sed -n "${1}p" "$GST_RATE_LOG"; }

# --- run ----------------------------------------------------------------------------------
set_rate 48000
RETURN_GST="$T/bin/gst-launch-1.0" RETURN_ASOUND_BASE="$T/asound" RETURN_DEST_IP=10.0.0.1 \
  RETURN_RATE_POLL_S=1 bash "$SCRIPT" > "$T/out.log" 2>&1 &
RUN=$!
sleep 2

[ "$(nth 1)" = "48000" ] && ok "starts at the rate the client negotiated (48000)" \
                         || no "expected first pipeline at 48000, got '$(nth 1)'"

# 1. steady state must NOT re-open the pipeline
sleep 3
[ "$(wc -l < "$GST_RATE_LOG" | tr -d ' ')" = "1" ] \
  && ok "no rate change -> pipeline is not restarted (got: $(rates))" \
  || no "pipeline thrashed with no rate change (got: $(rates))"

# 2. client switches 48k -> 44.1k: must follow
set_rate 44100
sleep 3
[ "$(nth 2)" = "44100" ] && ok "follows a live switch to 44100" \
                         || no "did not follow to 44100 (got: $(rates))"

# 3. client PAUSES (substream closed): must hold, not tear down
set_rate closed
sleep 3
[ "$(wc -l < "$GST_RATE_LOG" | tr -d ' ')" = "2" ] \
  && ok "client pause does not restart the pipeline (got: $(rates))" \
  || no "pause caused a restart (got: $(rates))"

# 4. resumes at a third rate
set_rate 32000
sleep 3
[ "$(nth 3)" = "32000" ] && ok "follows a live switch to 32000" \
                         || no "did not follow to 32000 (got: $(rates))"

# 5. clean shutdown releases the device (no orphan gst)
kill -TERM $RUN 2>/dev/null; sleep 1
pgrep -f "sleep 300" >/dev/null 2>&1 && no "orphan pipeline left after SIGTERM" \
                                     || ok "SIGTERM tears the pipeline down (no orphan)"

echo
echo "  rates seen: $(rates)"
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
