#!/bin/bash
# Fleet OTA update: pull the latest RepliKam2 and apply scripts/units.
# Config: /etc/default/bridge-update  (GIT_URL=https://<token>@github.com/Samithreddy72/RepliKam2.git)
set -e
[ -f /etc/default/bridge-update ] && . /etc/default/bridge-update
GIT_URL="${GIT_URL:-https://github.com/Samithreddy72/RepliKam2.git}"
D=/home/pi/replikam2-src
if [ -d "$D/.git" ]; then git -C "$D" pull -q --ff-only; else rm -rf "$D"; git clone -q --depth 1 "$GIT_URL" "$D"; fi
# apply scripts + units (NEVER touches the kernel or gadget config here)
cp "$D"/pi/scripts/* /usr/local/bin/ 2>/dev/null || true
[ -f /usr/local/bin/uvc-raw-setup.sh ] && mv /usr/local/bin/uvc-raw-setup.sh /home/pi/uvc-raw-setup.sh
cp -r "$D"/pi/systemd/* /etc/systemd/system/ 2>/dev/null || true
chmod +x /usr/local/bin/bridge* /usr/local/bin/*.sh 2>/dev/null || true
systemctl daemon-reload
# media-only restart (safe with client attached)
bridge restart >/dev/null 2>&1 || true
V=$(git -C "$D" rev-parse --short HEAD)
mkdir -p /etc/bridge && echo "2.0.0-$V" > /etc/bridge/version
echo "updated to $V"
