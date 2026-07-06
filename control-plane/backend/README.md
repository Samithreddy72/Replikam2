# NetBridge control plane — backend

FastAPI + SQLAlchemy. Aggregates fleet telemetry from each Pi's `bridge-agent`, exposes a
read-mostly admin API for the panel, and queues a small allow-list of safe remote commands.

## Run (dev)
```bash
cd control-plane/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export BOOTSTRAP_TOKENS=dev-boot ADMIN_API_KEY=dev-admin-key   # DATABASE_URL defaults to sqlite
uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000/docs for the OpenAPI UI.

## Deploy (prod)
Run the container on a cloud VM that is **joined to the tailnet** (so it can reach the Pis).
Use Postgres via `DATABASE_URL`. Put the admin panel + API behind HTTPS; only `/admin/*` and the
panel are public — `/v1/*` is reached by agents over the tailnet. Rotate `BOOTSTRAP_TOKENS`.

## Surfaces
- Device (Bearer device token; `/v1/enroll` uses a bootstrap token):
  `POST /v1/enroll`, `POST /v1/telemetry`, `GET /v1/commands`, `POST /v1/commands/{id}/result`
- Admin (Bearer `ADMIN_API_KEY`):
  `GET /admin/devices`, `GET /admin/devices/{id}`, `POST /admin/devices/{id}/claim`,
  `POST /admin/devices/{id}/commands`, `GET /admin/alerts`

Allowed command types: `restart`, `reset-clock`, `profile` (`{"mode":"lan|wan"}`),
`set-peer` (`{"ip":"100.x","port":"5004"}`). The Pi agent re-validates every command before running.

## Schema migrations
`create_all` bootstraps tables for dev. For prod, add Alembic before the first real deployment.
