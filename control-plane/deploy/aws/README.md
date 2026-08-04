# NetBridge control plane on AWS

One small always-on host: Caddy (automatic HTTPS) in front of the FastAPI control plane,
with SQLite on a Docker volume.

## Why this shape

The app runs a background alert loop every 30 s and keeps an on-disk database. Lambda has
neither. App Runner/Fargate would force Postgres (+RDS) and a load balancer for a single
small service. One box is the honest fit and stays trivially portable.

## The one decision that is expensive to undo

Bridges read `CONTROL_URL` from `/etc/default/bridge-agent` on the **read-only root**, so
changing it costs a card write **per device**. Point it at a **hostname you own** — then any
future rehost is a DNS record and no bridge is ever touched again.

**Ordering trap:** set the new `CONTROL_URL` while the OLD control plane is still reachable.
A bridge that cannot reach the old URL cannot be told about the new one.

## Bring-up

1. Instance: Ubuntu 22.04+, static IP, firewall open on 22/80/443 only.
   Port 80 must stay open — Caddy's certificate renewal uses it.
2. DNS: `fleet.<yourdomain>` A record → the static IP.
3. On the host: `sudo bash provision.sh`
4. From the Mac: `./deploy.sh fleet.<yourdomain>`
5. On the host: create `/opt/netbridge/.env` from `env.example` (chmod 600), then
   `cd /opt/netbridge && docker compose up -d`

## After it is up

- Turn on automatic snapshots. The database lives in the `fleetdata` volume; the snapshot
  is the actual backup.
- Keep the `caddydata` volume — it holds the certificates, and Let's Encrypt rate-limits
  re-issues.
- Leave `BOOTSTRAP_TOKENS` empty: the bootstrap key self-retires once a real admin exists,
  and an unconfigured deployment should be closed rather than open with a guessable key.

## Updating later

`./deploy.sh fleet.<yourdomain>` — builds locally, streams the image over ssh, restarts.
No registry, no CI dependency.
