#!/bin/sh
# This optional service is installed only on the diagnostic card, not factory images.
set -eu
mountpoint -q /data || { echo '/data is not mounted; refusing diagnostic SSH' >&2; exit 1; }
install -d -m 700 /data/diagnostics/ssh
install -d -m 755 /run/sshd
KEY=/data/diagnostics/ssh/ssh_host_ed25519_key
if [ ! -s "$KEY" ]; then
    umask 077
    ssh-keygen -q -t ed25519 -N '' -f "$KEY"
fi
/usr/sbin/sshd -t -f /etc/netbridge/diagnostics_sshd_config
exec /usr/sbin/sshd -D -e -f /etc/netbridge/diagnostics_sshd_config
