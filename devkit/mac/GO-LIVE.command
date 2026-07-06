#!/usr/bin/env bash
# RepliKam Developer GO-LIVE (Mac) — double-click to stream to YOUR bridge.
cd "$(dirname "$0")"
set -a; source ./developer.conf; set +a
export PI="$BRIDGE"
exec bash ./go-live.sh
