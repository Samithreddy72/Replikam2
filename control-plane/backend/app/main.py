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
from .models import Device, Telemetry, Command, utcnow
from .schemas import (EnrollIn, EnrollOut, CommandOut, CommandResultIn,
                      ClaimIn, IssueCommandIn)

ALLOWED_COMMANDS = {"restart", "reset-clock", "profile", "set-peer", "update", "reboot", "start", "stop", "diagnose", "set-pin", "unlock", "lock"}

app = FastAPI(title="NetBridge Control Plane", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Bootstrap tables for dev/first run. For prod, switch to Alembic migrations.
Base.metadata.create_all(engine)


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


# ----------------------------- admin panel (static) -----------------------------
# Serve the built React panel from the same process (declared last so API routes win).
import os as _os
from fastapi.staticfiles import StaticFiles

_static = _os.path.join(_os.path.dirname(__file__), "..", "static")
if _os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="panel")
