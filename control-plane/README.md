# Control plane

The fleet server: the web panel you manage bridges from, the API bridges report to, the
alerting loop, and the store of signed script payloads a bridge can fetch.

## Where it runs

**Deployed at `https://fleet.scine.online`** on AWS Lightsail. Everything needed to stand up
another one is in [`deploy/aws/`](deploy/aws/README.md) — read that first.

```bash
# from the repository root
bash control-plane/deploy/aws/deploy.sh <HOST_IP>
```

This sends the source, builds on the host and restarts. It takes about two minutes and does
**not** interrupt a live stream: media flows presenter → mesh → bridge peer-to-peer, so the
control plane only carries telemetry and commands.

> **Historical note:** this project previously deployed to Fly.io. Those files
> (`fly.toml`, `fly-secrets.sh`) were removed once the AWS deployment became the live one, so
> nobody follows them by mistake. They remain in git history.

## What is in here

| Path | What it is |
|---|---|
| `backend/` | FastAPI app — device API, admin API, alerting, retention, payload store |
| `panel-dist/` | The fleet web panel served at `/` |
| `deploy/aws/` | Everything to provision and deploy a control plane |

## Configuration

Secrets live in `/opt/netbridge/.env` on the host (chmod 600, never in git). See
[`deploy/aws/env.example`](deploy/aws/env.example) for every variable and what it does.

Two credentials are easy to confuse:

| Variable | Purpose |
|---|---|
| `ADMIN_API_KEY` | Creates the **first admin account** only. Set it, create the admin, then delete it — the code retires it automatically once an admin exists. |
| `BOOTSTRAP_TOKENS` | Lets a **device** exchange a bootstrap token for its own long-lived token during enrolment. |

## Adding a command a bridge can run

A command must pass **three** gates, and it fails silently-ish if any one is missing:

1. `ACTIONS` in `panel-dist/index.html` — so there is a button
2. `ALLOWED_COMMANDS` in `backend/app/main.py` — or the API rejects it with
   `unsupported command type` before it is ever queued
3. `ALLOWED` in `../pi/scripts/bridge-agent.py` — or the device refuses it

Miss the second one and the button appears, looks fine, and fails on first click.
