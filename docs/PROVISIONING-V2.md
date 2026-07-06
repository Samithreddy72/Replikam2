# Provisioning v2 — secret-free images, key-at-claim

**Status:** backend implemented + verified live (2026-07-07). Agent/firstboot consumption is
staged work — see "Not yet built" at the bottom.

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

`provision` is optional; omitting it keeps claim behaviour exactly as v1. Re-claiming with a
new `provision` restages a payload (e.g. to hand out a fresh key after a key expiry).

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

## Not yet built (next steps)

- **Agent-side consumption**: `bridge-agent.py` should call `/v1/provision` each tick and
  apply known keys (`tailscale_auth_key` first). Small, allow-listed applier — same pattern
  as the command allow-list.
- **Firstboot without tailscale**: the enroll call needs a network path before the mesh
  exists — either LAN-local discovery of the control plane or a temporary egress URL.
- **Generic image build**: strip the baked tailscale state from the golden image;
  firstboot generates hostname + shows the pairing code on the dashboard (already does).
- **Admin panel UI**: a "provision" field on the claim dialog.
