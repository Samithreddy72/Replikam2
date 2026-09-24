#!/bin/bash
# bridge-cmd-run.sh <command-id> -- <argv...>
#
# Runs ONE fleet command in its own systemd job (started by bridge-agent.py through systemd-run)
# and leaves the result where the agent reports it from on a later tick:
#   /data/agent-results/<id>.out   last 2000 bytes of stdout+stderr
#   /data/agent-results/<id>.rc    exit status, written LAST and atomically = "finished"
# Why: the agent is a oneshot that systemd kills after its start timeout, and its children died
# with it — a deploy or a media restart started inline could be cut off half-way with no result.
# Not updatable (it is part of the command path, see /etc/netbridge/updatable.conf).
set -uo pipefail
DIR="${BRIDGE_AGENT_RESULTS:-/data/agent-results}"
cid="${1:?usage: bridge-cmd-run.sh <id> -- <argv...>}"; shift
[ "${1:-}" = "--" ] && shift
case "$cid" in *[!A-Za-z0-9_-]*|"") echo "bridge-cmd-run: bad id" >&2; exit 64 ;; esac
[ $# -gt 0 ] || { echo "bridge-cmd-run: no command" >&2; exit 64; }
mkdir -p "$DIR"
out="$DIR/$cid.out"
"$@" > "$out" 2>&1
rc=$?
tail -c 2000 "$out" > "$out.tmp" 2>/dev/null && mv -f "$out.tmp" "$out"
echo "$rc" > "$DIR/$cid.rc.tmp" && mv -f "$DIR/$cid.rc.tmp" "$DIR/$cid.rc"
sync
exit 0
