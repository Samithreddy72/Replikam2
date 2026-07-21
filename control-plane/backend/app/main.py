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
from .models import Device, Telemetry, Command, DiagBundle, User, AuditLog, utcnow
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
    insp = _inspect(engine)
    def cols(t):
        try: return {c["name"] for c in insp.get_columns(t)}
        except Exception: return set()
    with engine.begin() as conn:
        if "provision" not in cols("devices"):
            conn.execute(_text("ALTER TABLE devices ADD COLUMN provision JSON"))
        # M5 org scoping: existing users/audit predate org_id — add it, default
        # 'default' so the current single-org fleet keeps working unchanged.
        if "users" in insp.get_table_names() and "org_id" not in cols("users"):
            conn.execute(_text("ALTER TABLE users ADD COLUMN org_id VARCHAR DEFAULT 'default'"))
        if "audit_log" in insp.get_table_names() and "org_id" not in cols("audit_log"):
            conn.execute(_text("ALTER TABLE audit_log ADD COLUMN org_id VARCHAR DEFAULT 'default'"))
        # M6 magic-link sign-in: one-time login code on the user row.
        if "users" in insp.get_table_names():
            ucols = cols("users")
            if "login_hash" not in ucols:
                conn.execute(_text("ALTER TABLE users ADD COLUMN login_hash VARCHAR"))
            if "login_expires" not in ucols:
                conn.execute(_text("ALTER TABLE users ADD COLUMN login_expires DATETIME"))
_migrate()


@app.on_event("startup")
async def _start_background():
    """Background loops: hourly retention sweep, and the alert evaluator that
    pushes new/cleared alerts out by email/webhook (walkthrough J4)."""
    import asyncio
    from .db import SessionLocal
    from . import retention, alerting
    asyncio.create_task(retention.sweep_loop(SessionLocal))
    asyncio.create_task(alerting.evaluate_loop(SessionLocal, settings.alert_eval_interval_s))


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
        # The bootstrap token decides which org the device enrolls into, so a
        # customer's cards land directly in their org (never visible to others).
        dev = Device(id=body.device_id, org_id=settings.bootstrap_tokens[body.bootstrap_token])
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
    # Retention is NOT done here any more — see retention.py. Pruning on the
    # write path meant a device that stopped reporting never got cleaned up.
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
def list_devices(actor=Depends(auth.require_viewer), db: Session = Depends(get_db)):
    devs = db.scalars(select(Device).where(Device.org_id == actor.org)
                      .order_by(Device.name.is_(None), Device.name)).all()
    return [_device_view(d) for d in devs]


@app.get("/admin/devices/{device_id}")
def device_detail(device_id: str, actor=Depends(auth.require_viewer),
                  db: Session = Depends(get_db)):
    dev = _scoped_device(db, device_id, actor)
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
def claim_device(device_id: str, body: ClaimIn, actor=Depends(auth.require_admin),
                 db: Session = Depends(get_db)):
    dev = _scoped_device(db, device_id, actor)
    dev.name = body.name
    if dev.claimed_at is None:
        dev.claimed_at = utcnow()
    if body.provision is not None:
        # Secret-at-claim: stage the one-time configure payload. The device's
        # next GET /v1/provision returns it once and the server forgets it.
        dev.provision = body.provision
    db.commit()
    _audit(db, actor, "claim", "%s -> %s" % (dev.pairing_code or device_id, body.name))
    return _device_view(dev)


@app.post("/admin/devices/{device_id}/commands")
def issue_command(device_id: str, body: IssueCommandIn, actor=Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    if body.type not in ALLOWED_COMMANDS:
        raise HTTPException(400, "unsupported command type")
    dev = _scoped_device(db, device_id, actor)
    c = Command(device_id=device_id, type=body.type, args=body.args or {})
    db.add(c)
    db.commit()
    # audit: never include args (set-pin/unlock carry the PIN)
    _audit(db, actor, "command:%s" % body.type, dev.name or device_id)
    return {"id": c.id, "status": c.status}


@app.post("/admin/commands/broadcast")
def broadcast_command(body: IssueCommandIn, actor=Depends(auth.require_admin),
                      db: Session = Depends(get_db)):
    """Queue the same command for every device IN THE CALLER'S ORG."""
    if body.type not in ALLOWED_COMMANDS:
        raise HTTPException(400, "unsupported command type")
    ids = []
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        c = Command(device_id=dev.id, type=body.type, args=body.args or {})
        db.add(c)
        db.flush()
        ids.append({"device": dev.name or dev.id, "command_id": c.id})
    db.commit()
    _audit(db, actor, "broadcast:%s" % body.type, "%d device(s)" % len(ids))
    return {"queued": ids}


@app.get("/admin/alerts")
def all_alerts(actor=Depends(auth.require_viewer), db: Session = Depends(get_db)):
    out = []
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        for a in device_alerts(dev):
            out.append({"device_id": dev.id, "name": dev.name, **a})
    return out


@app.get("/admin/devices/{device_id}/uptime")
def device_uptime(device_id: str, actor=Depends(auth.require_viewer),
                  db: Session = Depends(get_db)):
    """Uptime/SLA from telemetry ticks (~15s): minute-coverage over rolling 24h
    windows for the last 7 days, plus outage incidents (tick gaps > 120s).
    Powered-off time counts as down — that's the honest SLA.

    Reads RAW ticks for the recent window (minute-precise) and hourly ROLLUPS
    beyond it (rollup.py). up_minutes in a rollup is the same distinct-minute
    count this endpoint sums for raw, so a rolled day scores identically. The
    raw-keep window is a whole number of hours, so each 24h window falls entirely
    on one side of the boundary — no window is half raw, half rolled."""
    _scoped_device(db, device_id, actor)   # 404 if not in the caller's org
    from .models import TelemetryRollup
    now = utcnow().replace(tzinfo=None)
    since = now - dt.timedelta(days=7)
    naive = lambda t: t.replace(tzinfo=None) if t and t.tzinfo else t

    ts = [naive(r) for r in db.scalars(
        select(Telemetry.ts).where(Telemetry.device_id == device_id,
                                   Telemetry.ts > since).order_by(Telemetry.ts)).all()]
    rolls = db.execute(
        select(TelemetryRollup.hour, TelemetryRollup.up_minutes, TelemetryRollup.samples)
        .where(TelemetryRollup.device_id == device_id, TelemetryRollup.hour > since)
        .order_by(TelemetryRollup.hour)).all()

    # Raw gives exact per-minute coverage for the recent window; rollups give
    # per-hour up_minutes for older time. Raw and rollup cover DISJOINT regions
    # (raw is deleted once rolled), so a window's uptime = raw up-minutes in it +
    # rollup up_minutes of hours in it — no double counting. Aged windows are
    # accurate to the hour (a rollup hour is credited to the window holding its
    # start); recent windows stay minute-exact.
    up_min = {int((t - since).total_seconds() // 60) for t in ts}
    windows = []
    for w in range(7):          # w=0 newest (last 24h) … w=6 oldest
        lo = now - dt.timedelta(hours=24 * (w + 1))
        hi = now - dt.timedelta(hours=24 * w)
        lo_i = max(0, int((lo - since).total_seconds() // 60))
        hi_i = int((hi - since).total_seconds() // 60)
        total = max(1, hi_i - lo_i)
        up = sum(1 for m in up_min if lo_i <= m < hi_i) \
             + sum(um for hour, um, _ in rolls if lo <= naive(hour) < hi)
        windows.append({"ago_days": w, "pct": round(100.0 * min(up, total) / total, 1)})

    # Incidents: precise gaps from RAW (recent window). Older, coarse outages are
    # recovered from rollup hours that were not fully up (an hour with up_minutes
    # < 55 → a ~(60-up_minutes) min outage that hour) so a big old outage still
    # shows, at hour granularity. incidents[-5:] surfaces the newest, which are
    # almost always in the raw window anyway.
    incidents = []
    for hour, up_minutes, _ in rolls:
        if up_minutes < 55:
            incidents.append({"start": naive(hour).isoformat(),
                              "seconds": (60 - up_minutes) * 60, "coarse": True})
    for a, b in zip(ts, ts[1:]):
        gap = (b - a).total_seconds()
        if gap > 120:
            incidents.append({"start": a.isoformat(), "seconds": int(gap)})
    if ts and (now - ts[-1]).total_seconds() > 120:
        incidents.append({"start": ts[-1].isoformat(),
                          "seconds": int((now - ts[-1]).total_seconds()),
                          "ongoing": True})
    incidents.sort(key=lambda i: i["start"])

    if ts:
        last_end = None
        for i in incidents:
            if not i.get("ongoing"):
                e = dt.datetime.fromisoformat(i["start"]) + dt.timedelta(seconds=i["seconds"])
                last_end = e
        streak = int((now - (last_end or ts[0])).total_seconds())
    else:
        streak = 0
    total_ticks = len(ts) + sum(s for _, _, s in rolls)
    return {"windows": windows, "incidents": incidents[-5:], "streak_s": streak,
            "ticks_7d": total_ticks}


@app.get("/admin/devices/{device_id}/diagnostics")
def list_diagnostics(device_id: str, actor=Depends(auth.require_admin),
                     db: Session = Depends(get_db)):
    _scoped_device(db, device_id, actor)   # 404 if not in the caller's org
    rows = db.scalars(select(DiagBundle).where(DiagBundle.device_id == device_id)
                      .order_by(desc(DiagBundle.created_at))).all()
    return [{"id": b.id, "filename": b.filename, "size": b.size,
             "created_at": b.created_at.isoformat() if b.created_at else None} for b in rows]


@app.get("/admin/diagnostics/{bundle_id}")
def download_diagnostics(bundle_id: int, actor=Depends(auth.require_admin),
                         db: Session = Depends(get_db)):
    from fastapi import Response
    b = db.get(DiagBundle, bundle_id)
    if not b:
        raise HTTPException(404, "bundle not found")
    # A bundle belongs to a device — enforce that device is in the caller's org.
    bdev = db.get(Device, b.device_id)
    if not bdev or bdev.org_id != actor.org:
        raise HTTPException(404, "bundle not found")
    return Response(content=b.data, media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % b.filename})


def _audit(db: Session, actor, action: str, target: str | None = None):
    # actor may be an auth.Actor (has .email/.org) or a bare string+org for the
    # redeem path (no actor yet). Store both who and the org the action was in.
    if hasattr(actor, "email"):
        who, org = actor.email, actor.org
    else:
        who, org = str(actor), "default"
    db.add(AuditLog(who=who, org_id=org, action=action, target=target))
    db.commit()


def _scoped_device(db: Session, device_id: str, actor) -> Device:
    """Fetch a device the actor is allowed to see, or 404. Cross-org is answered
    identically to 'not found' so one org can't probe another's device ids."""
    dev = db.get(Device, device_id)
    if not dev or dev.org_id != actor.org:
        raise HTTPException(404, "device not found")
    return dev


@app.get("/admin/users")
def list_users(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    return [{"id": u.id, "email": u.email, "role": u.role,
             "pending_invite": u.invite_hash is not None,
             "last_seen": u.last_seen.isoformat() if u.last_seen else None}
            for u in db.scalars(select(User).where(User.org_id == actor.org)
                                .order_by(User.created_at)).all()]


@app.post("/admin/users")
def add_user(body: dict, actor=Depends(auth.require_admin),
             db: Session = Depends(get_db)):
    """Create a user IN THE ADMIN'S ORG + a one-time invite token. The invite is
    returned ONCE and never stored in plaintext — delivered offline (like PINs)."""
    import re as _re
    email = str(body.get("email") or "").strip().lower()
    role = str(body.get("role") or "presenter")
    if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "bad email")
    if role not in ("admin", "presenter"):
        raise HTTPException(400, "role must be admin or presenter")
    # Existence check scoped to the caller's org — a global check would let an
    # admin probe whether an email has an account in ANOTHER org (cross-tenant
    # enumeration). If the email exists in a different org, the DB's global-unique
    # constraint below still rejects it, but with the same generic 409 so the
    # caller can't tell which org (or that it's another org at all).
    if db.scalar(select(User).where(User.email == email, User.org_id == actor.org)):
        raise HTTPException(409, "user already exists")
    import secrets as _s
    from sqlalchemy.exc import IntegrityError
    invite = _s.token_urlsafe(24)
    db.add(User(email=email, org_id=actor.org, role=role, invite_hash=auth.hash_token(invite)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "user already exists")   # same message, no cross-org leak
    _audit(db, actor, "user:add", "%s (%s)" % (email, role))
    return {"email": email, "role": role, "invite": invite}


@app.delete("/admin/users/{uid}")
def revoke_user(uid: int, actor=Depends(auth.require_admin),
                db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.org_id != actor.org:
        raise HTTPException(404, "no such user")   # cross-org: indistinguishable from absent
    email = u.email
    db.delete(u)
    db.commit()
    _audit(db, actor, "user:revoke", email)
    return {"ok": True}


@app.post("/auth/redeem")
def redeem_invite(body: dict, db: Session = Depends(get_db)):
    """Exchange a one-time invite for the personal bearer token (shown once)."""
    invite = str(body.get("invite") or "")
    u = db.scalar(select(User).where(User.invite_hash == auth.hash_token(invite))) if invite else None
    if not u:
        raise HTTPException(401, "invalid or already-used invite")
    import secrets as _s
    token = _s.token_urlsafe(32)
    u.token_hash = auth.hash_token(token)
    u.invite_hash = None            # single use
    db.commit()
    _audit(db, auth.Actor(u.email, u.org_id, u.role), "user:redeem-invite")
    return {"email": u.email, "role": u.role, "token": token}


@app.post("/auth/magic-link")
def request_magic_link(body: dict, db: Session = Depends(get_db)):
    """Email a one-time sign-in code to an existing user (walkthrough J3: "A magic
    link signs you in"). ALWAYS returns 200 with the same body whether or not the
    email exists — so this can't be used to discover who has an account. The user
    must already exist (an admin added them); the code both signs them in and, if
    they'd never redeemed an invite, becomes their first login."""
    import re as _re, secrets as _s
    from .models import User, utcnow
    import threading
    from . import notifier
    email = str(body.get("email") or "").strip().lower()
    # SECURITY: the link host comes ONLY from server config, never the request.
    # Trusting a request-supplied base_url would let an attacker have the server
    # email a victim a VALID sign-in link pointing at the attacker's domain
    # (reset-poisoning → account takeover). If public_base_url is unset the email
    # carries just the paste-in code, which is all the app needs anyway.
    base = (settings.public_base_url or "").rstrip("/")
    generic = {"ok": True, "message": "If that email has an account, a sign-in link is on its way."}
    if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return generic
    u = db.scalar(select(User).where(User.email == email))
    if not u:
        return generic                      # no enumeration: same response
    code = _s.token_urlsafe(24)
    u.login_hash = auth.hash_token(code)
    u.login_expires = utcnow() + dt.timedelta(minutes=15)
    db.commit()
    link = ("%s/?code=%s" % (base, code)) if base else None
    lines = ["Sign in to NetBridge.", ""]
    if link:
        lines += ["Open this link to sign in:", link, ""]
    lines += ["Or paste this code into the NetBridge app:", "", "    %s" % code, "",
              "It expires in 15 minutes. If you didn't ask to sign in, ignore this email."]
    # Send in a background thread so the response time does NOT reveal whether the
    # account exists (a synchronous SMTP round-trip only on the found path would be
    # a timing side channel that defeats the no-enumeration guarantee above).
    def _send():
        try:
            notifier.send_mail(email, "Your NetBridge sign-in link", "\n".join(lines))
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()
    return generic


@app.post("/auth/magic-redeem")
def redeem_magic_link(body: dict, db: Session = Depends(get_db)):
    """Exchange a magic-link code for the personal bearer token. One use: the code
    is cleared on success. Rejects expired/unknown codes identically."""
    import secrets as _s
    from .models import User, utcnow
    code = str(body.get("code") or "").strip()
    u = db.scalar(select(User).where(User.login_hash == auth.hash_token(code))) if code else None
    now = utcnow()
    exp = u.login_expires if u else None
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=dt.timezone.utc)
    if not u or exp is None or exp < now:
        raise HTTPException(401, "invalid or expired code")
    token = _s.token_urlsafe(32)
    u.token_hash = auth.hash_token(token)
    u.login_hash = None                     # single use
    u.login_expires = None
    db.commit()
    _audit(db, auth.Actor(u.email, u.org_id, u.role), "user:magic-signin")
    return {"email": u.email, "role": u.role, "org": u.org_id, "token": token}


@app.get("/auth/whoami")
def whoami(actor=Depends(auth.require_viewer), db: Session = Depends(get_db)):
    if actor.is_bootstrap:
        return {"who": "bootstrap-key", "org": actor.org, "role": "admin",
                "bootstrap": True,
                "notice": "Shared bootstrap key — create an admin account "
                          "(Team → add user, role admin) and this key stops working."}
    return {"who": actor.email, "org": actor.org, "role": actor.role}


@app.get("/admin/audit")
def audit_log(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.org_id == actor.org)
                      .order_by(desc(AuditLog.ts)).limit(30)).all()
    return [{"who": a.who, "action": a.action, "target": a.target,
             "ts": a.ts.isoformat() if a.ts else None} for a in rows]


@app.get("/admin/alerts/history")
def alerts_history(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """Recent alert episodes (fired + resolved) for the caller's org, newest
    first — so an admin can see that a bridge flapped overnight even though no
    one had the panel open."""
    from .models import AlertEvent
    # Filter to the org's devices IN SQL before LIMIT — fetching the newest 200
    # globally then filtering in Python could return an EMPTY history for a quiet
    # org if noisier tenants produced 200 newer events (noisy-neighbor starvation).
    org_ids = [d.id for d in db.scalars(select(Device.id).where(Device.org_id == actor.org)).all()]
    if not org_ids:
        return []
    rows = db.scalars(select(AlertEvent).where(AlertEvent.device_id.in_(org_ids))
                      .order_by(desc(AlertEvent.opened_at)).limit(50)).all()
    return [{"device_id": e.device_id, "kind": e.kind, "detail": e.detail,
             "opened_at": e.opened_at.isoformat() if e.opened_at else None,
             "notified": e.notified_at is not None,
             "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None}
            for e in rows]


@app.post("/admin/alerts/test")
def alerts_test(who=Depends(auth.require_admin)):
    """Send a synthetic alert through every configured channel, so an admin can
    confirm their webhook/email is wired WITHOUT unplugging a bridge to trigger a
    real one. Reports exactly which channels fired."""
    from . import notifier
    if not notifier.any_channel_configured():
        raise HTTPException(400, "no alert channel configured — set ALERT_WEBHOOK_URL or SMTP_*")
    payload = notifier.build_message(
        "test-bridge", "TEST", "offline",
        "this is a NetBridge test alert sent by %s" % who, "firing",
        {"command": "none", "label": "no action — test only"})
    results = notifier.deliver(payload)
    return {"sent": results, "ok": any(results.values())}


# ----------------------------- admin panel (static) -----------------------------
# Serve the built React panel from the same process (declared last so API routes win).
import os as _os
from fastapi.staticfiles import StaticFiles

_static = _os.path.join(_os.path.dirname(__file__), "..", "static")
if _os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="panel")
