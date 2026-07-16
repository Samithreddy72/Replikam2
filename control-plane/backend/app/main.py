"""NetBridge fleet control plane API.

Two surfaces:
  /v1/*     device-facing  (agent: enroll, telemetry, command pull/result)
  /admin/*  operator-facing (admin panel: list/claim/rename, detail, issue command, alerts)

Single-org for now (admin API key). The backend itself runs as a node on the tailnet so it
can reach the Pis; only the admin panel is publicly exposed (behind the API key / future SSO).
"""
import datetime as dt

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from . import auth, models
from .alerts import device_alerts, is_online
from .models import Device, Telemetry, Command, DiagBundle, utcnow
from .schemas import (EnrollIn, EnrollOut, CommandOut, CommandResultIn,
                      ClaimIn, IssueCommandIn)

ALLOWED_COMMANDS = {"restart", "reset-clock", "profile", "set-peer", "update", "reboot", "start", "stop", "diagnose", "set-pin", "unlock", "lock"}

app = FastAPI(title="NetBridge Control Plane", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Bootstrap tables for dev/first run. For prod, switch to Alembic migrations.
Base.metadata.create_all(engine)

# Tiny in-code migration: create_all() never ALTERs existing tables, so add the
# columns that shipped after first deploy. Idempotent; sqlite-friendly.
def _migrate():
    from sqlalchemy import inspect as _inspect, text as _text
    cols = {c["name"] for c in _inspect(engine).get_columns("devices")}
    with engine.begin() as conn:
        if "provision" not in cols:
            conn.execute(_text("ALTER TABLE devices ADD COLUMN provision JSON"))
_migrate()


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ----------------------------- device-facing (/v1) -----------------------------

@app.post("/v1/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn, db: Session = Depends(get_db)):
    if body.bootstrap_token not in settings.bootstrap_tokens:
        raise HTTPException(401, "invalid bootstrap token")
    token, token_hash = auth.new_device_token()
    dev = db.get(Device, body.device_id)
    if dev is None:
        dev = Device(id=body.device_id)
        db.add(dev)
    dev.pairing_code = body.pairing_code
    dev.version = body.version
    dev.tailscale_ip = body.tailscale_ip
    dev.hostname = body.hostname
    dev.token_hash = token_hash  # re-enroll rotates the token
    db.commit()
    return EnrollOut(device_id=dev.id, device_token=token)


@app.post("/v1/telemetry")
def telemetry(body: dict, dev: Device = Depends(auth.require_device),
              db: Session = Depends(get_db)):
    now = utcnow()
    dev.last_seen = now
    dev.latest = body
    if body.get("version"):
        dev.version = body["version"]
    if body.get("tailscale_ip"):
        dev.tailscale_ip = body["tailscale_ip"]
    db.add(Telemetry(device_id=dev.id, ts=now, metrics=body))
    # retention: prune telemetry older than 7 days (~1-in-50 writes to keep it cheap)
    import random as _r
    if _r.random() < 0.02:
        cutoff = now - dt.timedelta(days=7)
        db.query(Telemetry).filter(Telemetry.ts < cutoff).delete()
    db.commit()
    return {"ok": True}


@app.get("/v1/commands", response_model=list[CommandOut])
def pull_commands(dev: Device = Depends(auth.require_device), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Command).where(Command.device_id == dev.id, Command.status == "pending")
        .order_by(Command.created_at)
    ).all()
    # At-most-once delivery: mark as "sent" the instant we hand them out, so a
    # command that disrupts the device before it can POST a result is NOT
    # re-pulled every tick (that once looped reset-clock -> gadget teardown).
    for c in rows:
        c.status = "sent"
    db.commit()
    return [CommandOut(id=c.id, type=c.type, args=c.args or {}) for c in rows]


@app.post("/v1/commands/{cmd_id}/result")
def command_result(cmd_id: int, body: CommandResultIn,
                   dev: Device = Depends(auth.require_device), db: Session = Depends(get_db)):
    c = db.get(Command, cmd_id)
    if not c or c.device_id != dev.id:
        raise HTTPException(404, "command not found")
    c.status = body.status
    c.output = body.output
    c.completed_at = utcnow()
    db.commit()
    return {"ok": True}


@app.get("/v1/provision")
def pull_provision(dev: Device = Depends(auth.require_device), db: Session = Depends(get_db)):
    """One-time provisioning payload (secret-at-claim, docs/PROVISIONING-V2.md).

    Returns whatever the admin attached at claim time (e.g. a tailscale auth key)
    and clears it in the same transaction, so the secret is handed out exactly
    once. Subsequent pulls get {"provision": null} — the agent treats that as
    "nothing to do", making it safe to poll every tick.
    """
    payload = dev.provision
    if payload is not None:
        dev.provision = None
        db.commit()
    return {"provision": payload}


@app.post("/v1/diagnostics")
def upload_diagnostics(body: dict, dev: Device = Depends(auth.require_device),
                       db: Session = Depends(get_db)):
    """Agent uploads a freshly collected diagnostics bundle (small tgz, base64).
    Keeps the newest 3 per device so the DB stays flat."""
    import base64, re
    filename = str(body.get("filename") or "")
    if not re.fullmatch(r"bundle-[0-9]{8}-[0-9]{6}\.tgz", filename):
        raise HTTPException(400, "bad bundle filename")
    try:
        data = base64.b64decode(body.get("data_b64") or "", validate=True)
    except Exception:
        raise HTTPException(400, "bad base64 payload")
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "bundle empty or over 5MB")
    db.add(DiagBundle(device_id=dev.id, filename=filename, size=len(data), data=data))
    keep = db.scalars(select(DiagBundle.id).where(DiagBundle.device_id == dev.id)
                      .order_by(desc(DiagBundle.created_at)).limit(3)).all()
    db.query(DiagBundle).filter(DiagBundle.device_id == dev.id,
                                ~DiagBundle.id.in_(keep)).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


# ----------------------------- operator-facing (/admin) -----------------------------

def _device_view(dev: Device) -> dict:
    return {
        "id": dev.id,
        "name": dev.name,
        "pairing_code": dev.pairing_code,
        "claimed": dev.claimed_at is not None,
        "hostname": dev.hostname,
        "version": dev.version,
        "tailscale_ip": dev.tailscale_ip,
        "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
        "online": is_online(dev),
        "latest": dev.latest,
        "alerts": device_alerts(dev),
    }


@app.get("/admin/devices")
def list_devices(_: bool = Depends(auth.require_admin), db: Session = Depends(get_db)):
    devs = db.scalars(select(Device).order_by(Device.name.is_(None), Device.name)).all()
    return [_device_view(d) for d in devs]


@app.get("/admin/devices/{device_id}")
def device_detail(device_id: str, _: bool = Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "device not found")
    view = _device_view(dev)
    rows = db.scalars(
        select(Telemetry).where(Telemetry.device_id == device_id)
        .order_by(desc(Telemetry.ts)).limit(120)
    ).all()
    view["telemetry"] = [{"ts": r.ts.isoformat(), "metrics": r.metrics} for r in reversed(rows)]
    cmds = db.scalars(
        select(Command).where(Command.device_id == device_id)
        .order_by(desc(Command.created_at)).limit(20)
    ).all()
    view["commands"] = [{"id": c.id, "type": c.type, "args": c.args, "status": c.status,
                         "output": c.output,
                         "created_at": c.created_at.isoformat()} for c in cmds]
    return view


@app.post("/admin/devices/{device_id}/claim")
def claim_device(device_id: str, body: ClaimIn, _: bool = Depends(auth.require_admin),
                 db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "device not found")
    dev.name = body.name
    if dev.claimed_at is None:
        dev.claimed_at = utcnow()
    if body.provision is not None:
        # Secret-at-claim: stage the one-time configure payload. The device's
        # next GET /v1/provision returns it once and the server forgets it.
        dev.provision = body.provision
    db.commit()
    return _device_view(dev)


@app.post("/admin/devices/{device_id}/commands")
def issue_command(device_id: str, body: IssueCommandIn, _: bool = Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    if body.type not in ALLOWED_COMMANDS:
        raise HTTPException(400, "unsupported command type")
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "device not found")
    c = Command(device_id=device_id, type=body.type, args=body.args or {})
    db.add(c)
    db.commit()
    return {"id": c.id, "status": c.status}


@app.post("/admin/commands/broadcast")
def broadcast_command(body: IssueCommandIn, _: bool = Depends(auth.require_admin),
                      db: Session = Depends(get_db)):
    """Queue the same command for every enrolled device (fleet-wide action)."""
    if body.type not in ALLOWED_COMMANDS:
        raise HTTPException(400, "unsupported command type")
    ids = []
    for dev in db.scalars(select(Device)).all():
        c = Command(device_id=dev.id, type=body.type, args=body.args or {})
        db.add(c)
        db.flush()
        ids.append({"device": dev.name or dev.id, "command_id": c.id})
    db.commit()
    return {"queued": ids}


@app.get("/admin/alerts")
def all_alerts(_: bool = Depends(auth.require_admin), db: Session = Depends(get_db)):
    out = []
    for dev in db.scalars(select(Device)).all():
        for a in device_alerts(dev):
            out.append({"device_id": dev.id, "name": dev.name, **a})
    return out


@app.get("/admin/devices/{device_id}/uptime")
def device_uptime(device_id: str, _: bool = Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    """Uptime/SLA from telemetry ticks (~15s): minute-coverage over rolling 24h
    windows for the last 7 days, plus outage incidents (tick gaps > 120s).
    Powered-off time counts as down — that's the honest SLA."""
    now = utcnow().replace(tzinfo=None)
    since = now - dt.timedelta(days=7)
    ts = [r for r in db.scalars(
        select(Telemetry.ts).where(Telemetry.device_id == device_id,
                                   Telemetry.ts > since).order_by(Telemetry.ts)).all()]
    ts = [t.replace(tzinfo=None) for t in ts]
    # minute coverage
    up_min = {int((t - since).total_seconds() // 60) for t in ts}
    windows = []
    for w in range(7):          # w=0 newest (last 24h) … w=6 oldest
        lo = now - dt.timedelta(hours=24 * (w + 1))
        hi = now - dt.timedelta(hours=24 * w)
        lo_i = max(0, int((lo - since).total_seconds() // 60))
        hi_i = int((hi - since).total_seconds() // 60)
        total = max(1, hi_i - lo_i)
        up = sum(1 for m in up_min if lo_i <= m < hi_i)
        windows.append({"ago_days": w, "pct": round(100.0 * up / total, 1)})
    # incidents: gaps between consecutive ticks > 120s (and an ongoing one)
    incidents = []
    for a, b in zip(ts, ts[1:]):
        gap = (b - a).total_seconds()
        if gap > 120:
            incidents.append({"start": a.isoformat(), "seconds": int(gap)})
    if ts and (now - ts[-1]).total_seconds() > 120:
        incidents.append({"start": ts[-1].isoformat(),
                          "seconds": int((now - ts[-1]).total_seconds()),
                          "ongoing": True})
    up_since = None
    if ts:
        up_since = (incidents[-1]["start"] if incidents and incidents[-1].get("ongoing")
                    else (ts[0].isoformat() if not incidents else None))
        # seconds since last completed incident = current clean streak
        last_end = None
        for i in incidents:
            if not i.get("ongoing"):
                e = dt.datetime.fromisoformat(i["start"]) + dt.timedelta(seconds=i["seconds"])
                last_end = e
        streak = int((now - (last_end or ts[0])).total_seconds())
    else:
        streak = 0
    return {"windows": windows, "incidents": incidents[-5:], "streak_s": streak,
            "ticks_7d": len(ts)}


@app.get("/admin/devices/{device_id}/diagnostics")
def list_diagnostics(device_id: str, _: bool = Depends(auth.require_admin),
                     db: Session = Depends(get_db)):
    rows = db.scalars(select(DiagBundle).where(DiagBundle.device_id == device_id)
                      .order_by(desc(DiagBundle.created_at))).all()
    return [{"id": b.id, "filename": b.filename, "size": b.size,
             "created_at": b.created_at.isoformat() if b.created_at else None} for b in rows]


@app.get("/admin/diagnostics/{bundle_id}")
def download_diagnostics(bundle_id: int, _: bool = Depends(auth.require_admin),
                         db: Session = Depends(get_db)):
    from fastapi import Response
    b = db.get(DiagBundle, bundle_id)
    if not b:
        raise HTTPException(404, "bundle not found")
    return Response(content=b.data, media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % b.filename})


# ----------------------------- admin panel (static) -----------------------------
# Serve the built React panel from the same process (declared last so API routes win).
import os as _os
from fastapi.staticfiles import StaticFiles

_static = _os.path.join(_os.path.dirname(__file__), "..", "static")
if _os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="panel")
