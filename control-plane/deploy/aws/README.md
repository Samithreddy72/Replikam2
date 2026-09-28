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
4. On the host: create `/opt/netbridge/.env` from `env.example` (chmod 600).
   It comes BEFORE the first deploy: without it every setting in the compose file is empty,
   so deploy.sh refuses to run (Caddy would start with no site address).
5. From the Mac: `DOMAIN=fleet.<yourdomain> ./deploy.sh fleet.<yourdomain>` - it builds, starts
   both containers and checks what is live. DOMAIN is the name those checks ask; it defaults
   to `fleet.scine.online`.

## After it is up

- Turn on automatic snapshots. The database lives in the `fleetdata` volume; the snapshot
  is the actual backup.
- Keep the `caddydata` volume — it holds the certificates, and Let's Encrypt rate-limits
  re-issues.
- Leave `BOOTSTRAP_TOKENS` empty: the bootstrap key self-retires once a real admin exists,
  and an unconfigured deployment should be closed rather than open with a guessable key.

## Updating later

`./deploy.sh fleet.<yourdomain>` — sends this commit's source, builds the image ON THE HOST,
switches to it and proves what is live. No registry, no CI dependency. In order:

1. Refuses a dirty tree, a host without `.env`, and a commit that does not contain what
   production runs, so an old worktree cannot silently undo later fixes. "What production
   runs" is read from the fleet container over ssh, so it is known even while the app is down.
   `ROLLBACK=1` deploys an older commit on purpose.
2. Builds `netbridge-fleet:<commit>` from a fresh copy of the source.
3. Backs up the database to `/data/bridge.db.pre-<commit>-<time>` - from the running app, or,
   when the app is down or crash-looping, with a one-off container of the image just built.
4. Validates the new compose file and Caddyfile, uploaded as `*.new` next to the live ones.
   A failure at any step up to here changes nothing that is live.
5. Only then switches: the build it replaces becomes `netbridge-fleet:previous` (when it was
   serving), the new one becomes `latest`, Caddy reloads, and the live commit, panel and
   `/admin/stream` are checked. If the app does not come up, the host's logs are printed.
   Older `netbridge-fleet:<commit>` tags are then removed, so only the two builds that latest
   and previous name stay on the host's disk.

## Rolling back

`./deploy.sh --rollback fleet.<yourdomain>` puts `netbridge-fleet:previous` back - the build
that was serving before the last deploy - without building anything or checking anything out.
A build that never served is never recorded as previous, so two failed deploys in a row still
roll back to the last good one.

To restore a database backup as well (the fleet is stopped while it is copied, and the database
it replaces is kept as `/data/bridge.db.pre-restore-<time>`):

    RESTORE_DB=bridge.db.pre-3f20cfa-20260925120000 ./deploy.sh --rollback fleet.<yourdomain>

The rollback leaves the compose file and Caddyfile alone; `/opt/netbridge/Caddyfile.prev` holds
the one from before the last deploy.

## Live deployment (2026-08-04)

| | |
|---|---|
| URL | `https://fleet.scine.online` |
| Host | Lightsail `netbridge-fleet`, Ubuntu 22.04, micro_3_0, us-east-1a, **$7/mo** |
| Static IP | `100.29.201.7` (DNS at GoDaddy) |
| SSH | `ssh -i ~/.ssh/netbridge-fleet.pem ubuntu@100.29.201.7` |
| Backups | auto-snapshot daily 07:00 UTC, 7 retained |
| Verified | reboot → HTTPS back unattended in 30 s, certificate survived |

The first admin was created with a temporary `ADMIN_API_KEY`, which was then removed — it is
one secret shared by every operator, so it should not outlive its single job.

**Not yet migrated:** bridges still point at the old control plane. See the ordering note above.

