# Optional maintenance access

These files are not automatically included in factory builds. The September 16 diagnostic
card uses a dedicated sshd on port 2222, accepting only the root public key explicitly
installed at `/etc/netbridge/diagnostics_authorized_keys`. Password login, interactive
password authentication, forwarding and tunnels are disabled. This is administrative
access: possession of the matching private key grants root command execution.

The private key remains on the owner's Mac in `~/.netbridge-source/keys/pi-diagnostics`.
Only the public key is installed on the card. Host keys persist in `/data/diagnostics/ssh`,
so sshd does not try to generate them on the read-only root. The service refuses to start
without the data partition mounted. It runs sshd's configuration check before starting.
Actual Pi boot, PAM/public-key authentication and audio capture still require validation.

The optional unit is `netbridge-diagnostics-ssh.service`. To disable diagnostic access:
`systemctl stop netbridge-diagnostics-ssh.service`; a persistent removal requires removing
its enablement symlink from the root image. Restoring the saved original rootA partition
also removes this access without touching `/data` or boot configuration.

Example after the Pi reconnects (verify its current IP):

```sh
ssh -p 2222 -i ~/.netbridge-source/keys/pi-diagnostics root@192.168.1.10
```

Do not disable host-key checking. Verify and retain the host fingerprint on first access.
After access works, inspect active signed overrides before assuming newly baked scripts
are in use. The existing signed-override verification mechanism is unchanged.
