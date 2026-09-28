# Provisioning v2 — secret-free images, key-at-claim

**Status:** backend + agent + firstboot implemented (2026-07-21). The card no longer
carries a tailnet key; the agent applies the claim-time key. ONE deployment
prerequisite remains before a keyless card can enroll in the field — a publicly
reachable control-plane URL. See "Remaining" at the bottom.

## The problem with v1

Today a bridge image is flashed with its secrets baked in: the tailscale auth key and the
fleet bootstrap token ride inside the SD card. That means:

- every flashed card is a credential leak waiting to happen (lost card = live tailnet key),
- keys expire, so images go stale on the shelf,
- one image cannot be mass-duplicated for a batch of units without sharing one key.

## The v2 flow: key-at-claim

The image contains **no per-customer secrets**. Identity is derived from hardware (the Pi CPU
serial), and the human-friendly pairing code is deterministic from it (`BRIDGE-XXXX`, printed
on the sticker — same scheme `bridge-web.py:pairing_code()` already uses).

```
 flash generic image          power on at customer site
        |                              |
        v                              v
  [device boots] --(1) enroll--> fleet-brain /v1/enroll  (bootstrap token*, gets device token)
        |                              |
        |   sticker/pairing code       v
  admin reads BRIDGE-2626 --(2)--> POST /admin/devices/{id}/claim
                                   { "name": "...", "provision":
                                     { "tailscale_auth_key": "tskey-auth-..." } }
        |                              |
        v                              v
  [next agent tick] --(3) GET /v1/provision --> returns payload ONCE, server clears it
        |
        v
  (4) device applies payload: `tailscale up --authkey=...` -> joins the mesh
```

(1) **Enroll** — unchanged from v1: the device announces `device_id` (CPU serial),
`pairing_code`, hostname/version, and receives its long-lived per-device token
(stored at `/etc/bridge/agent.token`, root-only). The generic bootstrap token only
authorizes *enrollment*, nothing else — it is the one shared value left in the image,
and it can be rotated server-side without reflashing (`settings.bootstrap_tokens` is a list).

(2) **Claim** — the operator sees the unclaimed device (pairing code matches the sticker)
and claims it, optionally attaching a `provision` JSON. This is the *only* moment the
secret exists in a request; it is stored on the Device row, and it is **never** echoed
back in any admin view (`_device_view` does not include it).

(3) **One-time handoff** — on its next tick the agent calls `GET /v1/provision`
(device-token auth). If a payload is staged, the server returns it and clears the column
in the same transaction. A second pull gets `{"provision": null}` — so the endpoint is
safe to poll every 15s tick, and the secret lives in the DB only for the seconds between
claim and the next tick.

(4) **Apply** — the agent hands the payload to a local applier (staged work, below):
`tailscale_auth_key` → `tailscale up --authkey=…`, future keys can carry Wi-Fi creds,
return-peer defaults, profile selection, etc.

## Backend API (implemented)

### `POST /admin/devices/{device_id}/claim`  (admin key)

Body (`ClaimIn`):

```json
{ "name": "bridge-001 (Samith)",
  "provision": { "tailscale_auth_key": "tskey-auth-..." } }
```

`provision` is optional; omitting it keeps claim behaviour exactly as v1. `name` must be 1-64
characters. A bridge is claimed once: claiming it again answers `409` (since 2026-09-28), because
the new key it staged made the bridge reset its mesh node and drop live sessions. To hand out a
fresh key, `POST /admin/devices/{device_id}/mesh-key` (optionally with
`{"tailscale_auth_key": "..."}` to stage a key made by hand); to rename, `PATCH /admin/devices/{device_id}`.

### `GET /v1/provision`  (device token)

- payload staged → `200 {"provision": {…}}` **and the server clears it**
- nothing staged → `200 {"provision": null}`
- bad/missing token → `401`

### Storage & migration

`Device.provision` (nullable JSON) in `control-plane/backend/app/models.py`. Existing
databases are upgraded by a tiny idempotent in-code migration in `main.py` (`_migrate()`:
`ALTER TABLE devices ADD COLUMN provision JSON` if missing) — no Alembic needed yet.

## Security notes

- The provision payload is **write-only from the admin side**: no admin endpoint returns it.
- It is handed out exactly once, to the holder of the device token only.
- It is not written to telemetry, logs, or the image.
- Residual risk: the payload sits in sqlite between claim and next tick (≤15s in practice);
  the DB lives on the control-plane host, which is already the trust anchor. Use
  short-expiry, pre-authorized, tagged tailscale keys so a leaked key has minimal blast radius.
- Rotating the bootstrap token invalidates stale images without touching enrolled devices.

## Verified live (2026-07-07, bridge-001 / 100000005d5ade42)

1. claim with `provision` → `200`, admin view does **not** contain the payload
2. `GET /v1/provision` with the device token → returned the staged payload
3. second `GET /v1/provision` → `{"provision": null}`
4. unauthenticated `GET /v1/provision` → `401`
5. agent telemetry unaffected (last_seen kept advancing after fleet-brain restart)

## Done (2026-07-21, M10)

- **Agent-side consumption** — `bridge-agent.py:apply_provision()` calls `/v1/provision`
  each tick and applies `tailscale_auth_key` (accepts the legacy `tailscale_authkey`
  spelling too — they had silently diverged, so a doc-following claim did nothing).
- **Firstboot no longer joins from a card key** — it only makes tailscaled ready; the
  join happens at claim via the delivered key. A legacy card's TS_AUTHKEY is ignored + warned.
- **Factory** — `make-card.sh` stops injecting `TS_AUTHKEY`; `fleet.conf.example` drops it.

## Remaining

- **⚠️ Public enrollment URL (the real blocker)**: a keyless card enrolls BEFORE it is on
  the tailnet, so `CONTROL_URL` must be reachable off-tailnet — a public `https://` endpoint
  (`tailscale serve` / reverse proxy + cert). Today it points at a tailnet IP (100.x), which
  deadlocks a keyless card. Until this exists, key-at-claim is code-complete but not
  operable in the field. Co-located dev (bridge-001, CONTROL_URL=127.0.0.1) is unaffected.
- **Generic image build**: strip baked tailscale state from the golden image so a clone
  boots unjoined (firstboot already generates hostname + shows the pairing code).
- **Admin panel UI**: a "provision" field on the claim dialog (today the key is supplied via
  the claim API body).
