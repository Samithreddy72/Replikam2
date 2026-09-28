"""NetBridge fleet control plane API.

Two surfaces:
  /v1/*     device-facing  (agent: enroll, telemetry, command pull/result)
  /admin/*  operator-facing (admin panel: list/claim/rename, detail, issue command, alerts)

Single-org for now (admin API key). The backend itself runs as a node on the tailnet so it
can reach the Pis; only the admin panel is publicly exposed (behind the API key / future SSO).
"""
import datetime as dt
import os
import re
import hashlib
import json
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from . import auth, models, notifier
from .alerts import device_alerts, is_online, alert_fix, bridge_title
from .models import (Device, Telemetry, Command, DiagBundle, User, AuditLog,
                     Rollout, RolloutTarget, utcnow)
from .schemas import (EnrollIn, EnrollOut, CommandOut, CommandResultIn,
                      ClaimIn, IssueCommandIn, RolloutCreateIn, DeviceUpdateIn)

# Must stay in step with the agent's own ALLOWED dict on the device. There are THREE
# gates a command passes — this one, the panel menu, and the device allow-list — and a
# command missing from any of them fails. Adding the recovery commands to the device and
# the panel was not enough: the API refused them here with "unsupported command type"
# before they were ever queued, so every new button would have failed on first click.
# No "set-peer" (2026-09-25 audit): it let any admin token point a room's microphone at any address
# with no PIN session. The return destination is set only by the presenter app, with its ticket.
ALLOWED_COMMANDS = {"restart", "reset-clock", "profile", "update", "reboot",
                    "start", "stop", "diagnose",
                    # PIN gate (2026-09-25): set a PIN, lift a brute-force lockout, end the live
                    # session. There is deliberately NO remote unlock: only a presenter typing the
                    # PIN opens a session. (The old unlock command is gone: on an old image it restarted media.)
                    "set-pin", "clear-lockout", "lock",
                    "deploy-script", "revert-script",
                    # remote recovery for a bridge nobody can physically reach
                    "unquarantine", "running", "logs",
                    # known-good configuration baseline (Golden Profile)
                    "golden-save", "golden-restore",
                    # jitter: diagnose the culprit, apply a ladder rung, or hand back
                    "jitter-diagnose", "jitter-fix", "jitter-reset",
                    # boot-time audio parameters (apply on next reboot)
                    "gadget-tune", "gadget-tune-show", "gadget-tune-clear",
                    # read-only look at the device's filesystem; the device enforces the
                    # roots and redacts anything credential-shaped
                    "read-file"}
# ---------------------------------------------------------------------------------------
# COMMAND POLICY — how long a command may take, and which ones can hurt.
#
# On 2026-08-24 a probe queued a real `reboot` against a live bridge and there was no way to
# recall it: no cancel endpoint, and the device had already collected it. The bridge rebooted.
# Two other commands from the same session sat in "sent" for over an hour while later ones
# completed, with nothing in the UI to say they were stuck.
#
# Two separate defences, because they solve different halves of that:
#   CONFIRM_REQUIRED  stops the dangerous ones being issued by accident in the first place,
#                     which is worth more than being able to cancel afterwards.
#   TIMEOUTS          guarantee every command reaches a terminal state, so "sent" can never
#                     again mean "unknown, forever".
#
# Timeouts are per CLASS, not global. A read answers in seconds; a reboot has to outlive the
# reboot itself; an OTA has to survive a large download over a venue uplink. One number would
# either kill legitimate slow work or leave a dead command sitting for an hour.
TIMEOUT_S = {
    # read-only: the agent answers on its next tick or something is wrong
    "running": 90, "logs": 90, "read-file": 90, "gadget-tune-show": 90,
    "jitter-diagnose": 120, "lock-state": 90,
    # diagnostics collect for ~40s on the device before uploading
    "diagnose": 300,
    # media restarts are quick; the gadget re-enumerates a client, which is slower
    "restart": 180, "profile": 180, "jitter-fix": 180, "jitter-reset": 180,
    "set-peer": 120, "set-pin": 120, "clear-lockout": 120, "lock": 120,
    "gadget-tune": 120, "gadget-tune-clear": 120,
    "golden-save": 180, "golden-restore": 300,
    # must outlive the reboot and the services coming back
    "reboot": 420, "reset-clock": 300,
    # script deployment restarts a service and may auto-rollback
    "deploy-script": 420, "revert-script": 300, "unquarantine": 300,
    # a whole image over whatever uplink the venue has
    "update": 3600,
}
DEFAULT_TIMEOUT_S = 240

# Issuing these interrupts a meeting, changes what code runs, or cannot be undone from the
# panel. The FRONTEND already warns; that is not a control, because the API is reachable
# without it - which is exactly how the accidental reboot happened. The backend now refuses
# them unless the caller states the intent explicitly.
CONFIRM_REQUIRED = {"reboot", "update", "deploy-script", "revert-script",
                    "unquarantine", "golden-restore", "reset-clock",
                    # ends the live session at once: the presenter's video stops arriving
                    "lock"}

# Commands where running the same thing twice is materially worse than running it once, so an
# identical one already pending or sent is handed back rather than queued again. Read-only
# commands are absent on purpose: asking twice is free, and an operator refreshing diagnostics
# should get a fresh answer instead of a stale row.
NO_DOUBLE_EXECUTE = {"reboot", "restart", "update", "deploy-script", "revert-script",
                     "golden-restore", "golden-save", "unquarantine", "reset-clock",
                     "set-pin", "profile", "gadget-tune", "gadget-tune-clear"}

# A command in one of these has finished as far as the control plane is concerned. Nothing a
# device says afterwards may overwrite it -- see command_result(). `pending` and `sent` are the
# only states from which a result is accepted.
TERMINAL_STATES = models.COMMAND_TERMINAL_STATES   # one set, shared with the retention sweep


def _refuse_by_policy(body) -> None:
    """Commands the fleet refuses whatever the client says. The LAN profile is one: the owner's
    standing rule is that bridges stay on WAN (latency is cut in the video feeder instead), and a
    profile switch restarts the whole media stack — which rebooted an under-powered bridge 4/4."""
    if body.type == "profile" and str((body.args or {}).get("mode", "")).strip().lower() == "lan":
        raise HTTPException(400, {"error": "LAN profile disabled",
                                  "detail": "bridges stay on the WAN profile (owner's rule)"})


def _pin_protocol(dev) -> int:
    """2 = the bridge's PIN gate from the 2026-09-25 image; 1 = older software."""
    pin = (dev.latest or {}).get("pin") if isinstance(dev.latest, dict) else None
    try:
        return int((pin or {}).get("protocol") or 1) if isinstance(pin, dict) else 1
    except (TypeError, ValueError):
        return 1


OLD_SOFTWARE_REFUSALS = {
    # On older bridge software `lock` runs `bridge stop`, stopping the camera service - with a meeting
    # laptop attached that has rebooted bridges - and the panel would have promised "media keeps running".
    "lock": "this bridge runs software from before 2026-09-25: its lock STOPS ALL MEDIA (camera "
            "included - with a laptop attached that has rebooted bridges). Update the bridge first.",
    "clear-lockout": "this bridge runs software from before 2026-09-25, which has no clear-lockout: "
                     "its lockout ends by itself within an hour. Update the bridge to clear it remotely.",
}


def _refuse_for_old_software(dev, ctype: str) -> None:
    if ctype in OLD_SOFTWARE_REFUSALS and _pin_protocol(dev) < 2:
        raise HTTPException(409, OLD_SOFTWARE_REFUSALS[ctype])


def _timeout_for(ctype: str) -> int:
    return TIMEOUT_S.get(ctype, DEFAULT_TIMEOUT_S)


def _sweep_expired(db: Session) -> int:
    """Move commands that were delivered and never answered into a terminal state.

    Called from the read paths rather than a background task: the fleet is small, the query
    is indexed, and a sweeper that only runs when someone is looking cannot itself become a
    silent failure. A command is only expired once it has actually been DELIVERED - a row
    still `pending` is waiting for the device to poll, which is not a fault.
    """
    now = utcnow()
    stale = db.scalars(select(Command).where(Command.status == "sent")).all()
    n = 0
    for c in stale:
        started = c.sent_at or c.created_at
        if not started:
            continue
        # tz-naive rows exist in databases written by the previous build
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
        if (now - started).total_seconds() > (c.timeout_s or DEFAULT_TIMEOUT_S):
            c.status = "expired"
            _scrub_secret_args(c)
            c.fail_reason = ("no result within %ds of delivery — the device may have rebooted, "
                             "lost its uplink, or died mid-command" % (c.timeout_s or DEFAULT_TIMEOUT_S))
            c.completed_at = now
            n += 1
    if n:
        db.commit()
    return n


# Commands whose args contain a secret. Their args are scrubbed as soon as the command reaches
# ANY final state - done, failed, rejected, expired, cancelled, or a late report - so a PIN never
# lives in the fleet database beyond its delivery window. (Until 2026-09-25 only a normal result
# scrubbed it; an expired or cancelled set-pin kept the PIN in plaintext forever.)
PIN_BEARING_COMMANDS = {"set-pin", "unlock"}


def _scrub_secret_args(c) -> None:
    if c.type in PIN_BEARING_COMMANDS and (c.args or {}) != {"_scrubbed": True}:
        c.args = {"_scrubbed": True}

# LAN-only mode: when the tailnet/Funnel is unreachable on the deployment's network
# (e.g. an ISP that drops the Tailscale handshake), a bridge's advertised mesh IP is a
# dead address. If the app is handed that IP it tries the mesh first and hangs ~45s
# ("unlock timed out") before falling back to direct LAN. With NB_LAN_ONLY=1 the fleet
# simply never stores/advertises a mesh IP, so the app always takes the reachable
# direct-LAN route (it reads the bridge's LAN IP from telemetry `latest.ip`). Reversible:
# unset the env var and let the next telemetry re-populate the mesh IP once mesh works.
LAN_ONLY = os.getenv("NB_LAN_ONLY", "").strip().lower() not in ("", "0", "false", "no", "off")

# INTERACTIVE API DOCS ARE OFF UNLESS SOMEBODY ASKS FOR THEM.
#
# FastAPI serves /docs, /redoc and /openapi.json to anyone, with no authentication, by default.
# On 26 Aug 2026 the live control plane was disclosing all 35 endpoints -- 23 admin routes and
# 5 auth routes -- to an unauthenticated visitor, with no securitySchemes declared.
#
# The endpoints themselves are gated (/admin/rollouts correctly answers 401), so this was
# reconnaissance value rather than direct access. But publishing the complete shape of an admin
# API to the internet buys an attacker a map for free and buys us nothing: the people who need
# the schema are developers, who can set the flag.
#
# NB_API_DOCS=1 turns them back on for local development.
_DOCS = os.getenv("NB_API_DOCS", "").strip().lower() in ("1", "true", "yes", "on")

app = FastAPI(
    title="NetBridge Control Plane",
    version="0.1.0",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
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
        # Setup-AP passphrase column (build-ledger E1) on existing fleet databases.
        if "devices" in insp.get_table_names():
            if "setup_pass" not in cols("devices"):
                conn.execute(_text("ALTER TABLE devices ADD COLUMN setup_pass VARCHAR"))
            # Fleet numbers (NB-001 …), 2026-09-24.
            if "number" not in cols("devices"):
                conn.execute(_text("ALTER TABLE devices ADD COLUMN number INTEGER"))
        # M6 magic-link sign-in: one-time login code on the user row.
        if "users" in insp.get_table_names():
            ucols = cols("users")
            if "login_hash" not in ucols:
                conn.execute(_text("ALTER TABLE users ADD COLUMN login_hash VARCHAR"))
            if "login_expires" not in ucols:
                conn.execute(_text("ALTER TABLE users ADD COLUMN login_expires DATETIME"))
        # Command lifecycle (2026-08-25). Existing rows keep their status; the new columns are
        # nullable or defaulted, so a fleet database written by the previous build upgrades in
        # place with no data loss. timeout_s defaults to the conservative class value rather
        # than 0, so an old row inherited by the sweeper is never expired the instant it loads.
        if "commands" in insp.get_table_names():
            ccols = cols("commands")
            if "idempotency_key" not in ccols:
                # Added 2026-08-26 with the retry-safety work. create_all() never ALTERs an
                # existing table, so without this the deployed control plane keeps a commands
                # table with no such column and every insert fails.
                conn.execute(_text("ALTER TABLE commands ADD COLUMN idempotency_key VARCHAR"))
            if "sent_at" not in ccols:
                conn.execute(_text("ALTER TABLE commands ADD COLUMN sent_at DATETIME"))
            if "timeout_s" not in ccols:
                conn.execute(_text("ALTER TABLE commands ADD COLUMN timeout_s INTEGER DEFAULT 120"))
            if "fail_reason" not in ccols:
                conn.execute(_text("ALTER TABLE commands ADD COLUMN fail_reason VARCHAR"))
_migrate()


def fleet_label(number):
    """NB-001 style label for a fleet number (None while a bridge has no number)."""
    return ("NB-%03d" % number) if number else None


def _next_number(db: Session, org: str) -> int:
    used = [n for n in db.scalars(select(Device.number).where(Device.org_id == org)).all() if n]
    return (max(used) + 1) if used else 1


def _backfill_numbers():
    """Give every CLAIMED bridge a fleet number, in the order they were claimed.

    Fleets older than numbering get theirs here, once; new claims take the next free number in
    claim_device(). A number never changes on its own — only an admin renumbers (PATCH)."""
    from .db import SessionLocal
    db = SessionLocal()
    try:
        devs = db.scalars(select(Device)).all()
        changed = False
        for org in sorted({d.org_id for d in devs}):
            mine = [d for d in devs if d.org_id == org]
            nxt = max([d.number for d in mine if d.number] or [0]) + 1
            for d in sorted((d for d in mine if d.claimed_at is not None and not d.number),
                            key=lambda d: (str(d.claimed_at), d.id)):
                d.number, nxt, changed = nxt, nxt + 1, True
        if changed:
            db.commit()
    finally:
        db.close()


_backfill_numbers()


def _scrub_old_secrets():
    """Once per start: PINs left in final-state rows by builds before the 2026-09-25 fix."""
    from .db import SessionLocal
    db = SessionLocal()
    try:
        n = 0
        for c in db.scalars(select(Command).where(Command.type.in_(PIN_BEARING_COMMANDS),
                                                  Command.status.in_(TERMINAL_STATES))).all():
            if (c.args or {}) != {"_scrubbed": True}:
                _scrub_secret_args(c)
                n += 1
        if n:
            db.commit()
            print("[secrets] scrubbed the PIN from %d finished command row(s)" % n)
    finally:
        db.close()


_scrub_old_secrets()


@app.on_event("startup")
async def _start_background():
    """Background loops: hourly retention sweep, and the alert evaluator that
    pushes new/cleared alerts out by email/webhook (walkthrough J4)."""
    import asyncio
    from .db import SessionLocal
    from . import retention, alerting
    asyncio.create_task(retention.sweep_loop(SessionLocal))
    asyncio.create_task(alerting.evaluate_loop(SessionLocal, settings.alert_eval_interval_s))


# WHAT COMMIT IS ACTUALLY LIVE?
#
# Until 26 Aug 2026 the answer was "nobody can tell". The backend reported no version, no
# commit and no build id, so the only way to establish what fleet.scine.online was running was
# to probe its BEHAVIOUR -- which is exactly how the /docs exposure was found still live days
# after being closed in source. "Fixed in the repository" and "fixed in production" were
# indistinguishable from the outside, which is the same disease that left the P0 auth fix off
# the bridge.
#
# deploy.sh now stamps these at build time. An unstamped deployment reports "unknown" rather
# than inventing a value: a control plane that cannot say what it is must not claim to be
# current.
CP_GIT_SHA = os.getenv("CONTROL_PLANE_GIT_SHA", "unknown")
CP_BUILD_ID = os.getenv("CONTROL_PLANE_BUILD_ID", "unknown")
CP_BUILT_AT = os.getenv("CONTROL_PLANE_BUILT_AT", "unknown")


@app.get("/healthz")
def healthz():
    """Liveness AND identity.

    `{"ok": true}` alone is the kind of green light this project has learned to distrust: it
    means a process answered a socket, not that the right code is running.
    """
    return {
        "ok": True,
        "git_sha": CP_GIT_SHA,
        "git_short": CP_GIT_SHA[:7] if CP_GIT_SHA != "unknown" else "unknown",
        "build_id": CP_BUILD_ID,
        "built_at": CP_BUILT_AT,
        "api_docs_public": _DOCS,
    }


# ----------------------------- device-facing (/v1) -----------------------------

@app.post("/v1/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn, db: Session = Depends(get_db)):
    if body.bootstrap_token not in settings.bootstrap_tokens:
        raise HTTPException(401, "invalid bootstrap token")
    token, token_hash = auth.new_device_token()
    dev = db.get(Device, body.device_id)
    is_new = dev is None
    if is_new:
        # The bootstrap token decides which org the device enrolls into, so a
        # customer's cards land directly in their org (never visible to others).
        dev = Device(id=body.device_id, org_id=settings.bootstrap_tokens[body.bootstrap_token])
        db.add(dev)
    dev.pairing_code = body.pairing_code
    dev.version = body.version
    dev.tailscale_ip = None if LAN_ONLY else body.tailscale_ip
    dev.hostname = body.hostname
    dev.token_hash = token_hash  # re-enroll rotates the token
    db.commit()
    # A brand-new device_id = a new SD card contacting the fleet for the first time.
    # That is not a "fault" the level-triggered alert loop would ever catch (a healthy
    # card firing nothing), so page it here, once, as its own edge event.
    if is_new:
        _notify_new_device(db, dev)
    return EnrollOut(device_id=dev.id, device_token=token)


def _notify_new_device(db, dev):
    """Email/webhook a one-time 'new SD card enrolled' alert. Best-effort: a delivery
    failure must never break enrollment (the card still gets its token)."""
    import logging
    from .models import AlertEvent
    log = logging.getLogger("main")
    try:
        detail = "new SD card enrolled: %s (v%s)" % (dev.hostname or dev.id, dev.version or "?")
        ev = AlertEvent(device_id=dev.id, kind="new_device", detail=detail)
        db.add(ev)
        db.flush()
        if notifier.any_channel_configured():
            payload = notifier.build_message(
                bridge_title(dev), dev.id,
                "new_device", detail, "firing", alert_fix("new_device", dev))
            if any(notifier.deliver(payload).values()):
                ev.notified_at = utcnow()
        db.commit()
    except Exception:
        log.exception("new-device alert failed for %s", getattr(dev, "id", "?"))
        db.rollback()


@app.post("/v1/telemetry")
def telemetry(body: dict, dev: Device = Depends(auth.require_device),
              db: Session = Depends(get_db)):
    now = utcnow()
    # Strip the label secret out of the blob FIRST: `latest` is returned by every device
    # view and the blob is also written to the retained Telemetry table. It belongs in its
    # own column, read back only through /admin/devices/{id}/label.
    sp = body.pop("setup_pass", None)
    if sp:
        dev.setup_pass = sp
    dev.last_seen = now
    dev.latest = body
    if body.get("version"):
        dev.version = body["version"]
    if LAN_ONLY:
        dev.tailscale_ip = None      # keep the dead mesh IP from creeping back in
    else:
        # The DEVICE is authoritative about its own mesh identity, including its ABSENCE.
        # This used to only write a truthy value, so a bridge that lost its tailnet node
        # (ephemeral nodes are garbage-collected after an outage) kept advertising its old
        # 100.x address forever: the panel showed it "on the mesh", the presenter app was
        # handed a dead IP, and nothing could tell the difference between a healthy bridge
        # and one that had silently fallen off. Clearing it makes the state honest — and is
        # what lets /v1/provision notice the device needs a fresh key and self-heal.
        dev.tailscale_ip = body.get("tailscale_ip") or None
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
        # The clock the timeout runs against. Without it, "how long has this been out?" could
        # only be answered from created_at, which includes however long the device was offline
        # before it polled - and would expire commands that were never actually delivered late.
        c.sent_at = utcnow()
    db.commit()
    return [CommandOut(id=c.id, type=c.type, args=c.args or {}) for c in rows]


@app.post("/v1/commands/{cmd_id}/result")
def command_result(cmd_id: int, body: CommandResultIn,
                   dev: Device = Depends(auth.require_device), db: Session = Depends(get_db)):
    c = db.get(Command, cmd_id)
    if not c or c.device_id != dev.id:
        raise HTTPException(404, "command not found")

    # TERMINAL STATES ARE FINAL. LATE NEWS IS RECORDED, NOT SUBSTITUTED.
    #
    # This was `c.status = body.status`, unconditionally, which let a device report rewrite a
    # verdict the control plane had already reached. The dangerous direction is not the obvious
    # one: a command CANCELLED by an operator, or EXPIRED because the deadline passed, could be
    # turned into `done` minutes later by an agent that finally got around to answering. The
    # panel would then show a green tick for a command the operator believes they stopped.
    #
    # A device finishing after the deadline is a real event and worth knowing. It is just not
    # the same event as "this succeeded", and the two must not be conflated. So the original
    # verdict stands and the late report is appended as evidence -- both facts survive, which
    # is what an operator needs to reconstruct what actually happened.
    if c.status in TERMINAL_STATES:
        _scrub_secret_args(c)
        stamp = utcnow().isoformat(timespec="seconds")
        late = ("\n--- late report from the device at %s: status=%s "
                "(the control plane had already recorded '%s'; that verdict stands) ---\n%s"
                % (stamp, body.status, c.status, body.output or ""))
        c.output = (c.output or "") + late
        db.commit()
        return {"ok": True, "recorded": "late-report", "status": c.status,
                "note": "command already %s; the device's later result was appended as "
                        "evidence and did not change the verdict" % c.status}

    c.status = body.status
    c.output = body.output
    c.completed_at = utcnow()
    # set-pin / unlock carry the PIN in args. The audit log already omits args, but the
    # Command row kept them in PLAINTEXT FOREVER - so the fleet DB accumulated every PIN
    # ever issued, contradicting "PINs travel offline; the panel never displays one".
    # The device has executed it by now, so the value has no further use here: scrub it.
    if c.type in PIN_BEARING_COMMANDS:
        c.args = {"_scrubbed": True}
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

    # SELF-HEAL: a claimed bridge that is talking to us but has NO mesh identity has lost
    # its tailnet node — bridge nodes are ephemeral, so any outage long enough for the
    # control plane to garbage-collect the node leaves the device with no key and no way
    # to rejoin. It used to take a manual re-key (or SD-card surgery) every single time,
    # which is absurd for a device that is plainly online and authenticated right here.
    # Mint one for it automatically; it applies the key on this same poll cycle (~15 s).
    if dev.claimed_at and not dev.tailscale_ip:
        from . import mesh
        try:
            minted = mesh.mint_ephemeral_key(
                "netbridge bridge %s" % (dev.pairing_code or dev.id),
                tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
        except mesh.MeshNotConfigured:
            return {"provision": None}          # fleet has no tailnet: nothing to hand out
        except Exception:
            return {"provision": None}          # tailnet unreachable: retry on the next poll
        if minted.get("key"):
            prov = {"tailscale_auth_key": minted["key"]}
            code = (dev.pairing_code or "").replace("BRIDGE-", "").strip()
            if code:
                prov["tailscale_hostname"] = "netbridge-%s" % code
            # Audited as the system, not a person — nobody clicked anything.
            _audit(db, auth.Actor("system", dev.org_id, "admin"),
                   "mesh-key:auto-reissue", dev.name or dev.pairing_code or dev.id)
            db.commit()
            return {"provision": prov}
    return {"provision": None}


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
    # Flush first. The session runs with autoflush=False, so the query below could not see the
    # row just added: "the newest 3" were the 3 newest OLD bundles, the new one was inserted on
    # commit, and every device kept 4 (up to 20 MB inline in SQLite) instead of 3 (2026-09-28).
    db.flush()
    keep = db.scalars(select(DiagBundle.id).where(DiagBundle.device_id == dev.id)
                      .order_by(desc(DiagBundle.created_at), desc(DiagBundle.id)).limit(3)).all()
    db.query(DiagBundle).filter(DiagBundle.device_id == dev.id,
                                ~DiagBundle.id.in_(keep)).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


# ----------------------------- operator-facing (/admin) -----------------------------

def _device_state(dev: Device, online: bool, alerts: list) -> str:
    """One word for "how is this bridge right now" — the same in the panel, nb and alerts.

      new       joined the fleet, not claimed yet
      offline   no heartbeat within OFFLINE_AFTER_S
      live      a presenter's video is flowing through it right now
      degraded  online and idle, but something needs attention (any alert)
      active    online, healthy, idle — ready for a presenter"""
    if dev.claimed_at is None:
        return "new"
    if not online:
        return "offline"
    t = dev.latest if isinstance(dev.latest, dict) else {}
    streams = t.get("streams") if isinstance(t.get("streams"), dict) else {}
    if streams.get("video"):
        return "live"
    return "degraded" if alerts else "active"


def _safe_alerts(dev: Device) -> list:
    try:
        return device_alerts(dev)
    except Exception as e:                  # malformed telemetry from one bridge
        print("[alerts] could not evaluate %s: %r" % (dev.id, e))
        return [{"kind": "telemetry_unreadable", "title": notifier.alert_title("telemetry_unreadable"),
                 "severity": notifier._severity("telemetry_unreadable"),
                 "detail": "this bridge sent telemetry the fleet could not read",
                 "fix": alert_fix("telemetry_unreadable", dev)}]


def _device_view(dev: Device) -> dict:
    online = is_online(dev)
    alerts = _safe_alerts(dev)
    t = dev.latest if isinstance(dev.latest, dict) else {}
    return {
        "id": dev.id,
        "number": dev.number,
        "label": fleet_label(dev.number),
        "name": dev.name,
        "pairing_code": dev.pairing_code,
        "claimed": dev.claimed_at is not None,
        "hostname": dev.hostname,
        "version": dev.version,
        "tailscale_ip": dev.tailscale_ip,
        "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
        "online": online,
        "state": _device_state(dev, online, alerts),
        # the meeting laptop is plugged in and has enumerated the USB camera/mic/speaker
        "laptop": (t.get("udc") == "configured") if online else None,
        "latest": dev.latest,
        "alerts": alerts,
    }


@app.get("/admin/devices")
def list_devices(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    devs = db.scalars(select(Device).where(Device.org_id == actor.org)
                      .order_by(Device.name.is_(None), Device.name)).all()
    return [_device_view(d) for d in devs]


@app.get("/admin/devices/{device_id}")
def device_detail(device_id: str, actor=Depends(auth.require_admin),
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
    view["commands"] = [{"id": c.id, "type": c.type, "status": c.status,
                         "args": {} if c.type in PIN_BEARING_COMMANDS else c.args,
                         "output": c.output,
                         "created_at": c.created_at.isoformat()} for c in cmds]
    return view


@app.patch("/admin/devices/{device_id}")
def update_device(device_id: str, body: DeviceUpdateIn, actor=Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    """Rename a bridge or change its fleet number (NB-###). Admin only; audited."""
    dev = _scoped_device(db, device_id, actor)
    changes = []
    if body.name is not None:
        name = body.name.strip()
        if not 1 <= len(name) <= 64:
            raise HTTPException(400, "name must be 1-64 characters")
        if name != dev.name:
            changes.append("name %r -> %r" % (dev.name, name))
            dev.name = name
    if body.number is not None:
        if not 1 <= body.number <= 9999:
            raise HTTPException(400, "number must be between 1 and 9999")
        taken = db.scalar(select(Device).where(Device.org_id == dev.org_id,
                                               Device.number == body.number,
                                               Device.id != dev.id))
        if taken is not None:
            raise HTTPException(409, "%s is already %s" % (fleet_label(body.number),
                                                           taken.name or taken.id))
        if body.number != dev.number:
            changes.append("number %s -> %s" % (fleet_label(dev.number), fleet_label(body.number)))
            dev.number = body.number
    if changes:
        db.commit()
        _audit(db, actor, "device:update", "%s: %s" % (dev.name or dev.id, "; ".join(changes)))
    return _device_view(dev)


@app.post("/admin/devices/{device_id}/claim")
def claim_device(device_id: str, body: ClaimIn, actor=Depends(auth.require_admin),
                 db: Session = Depends(get_db)):
    dev = _scoped_device(db, device_id, actor)
    dev.name = body.name
    if dev.claimed_at is None:
        dev.claimed_at = utcnow()
    if not dev.number:
        dev.number = _next_number(db, dev.org_id)
    prov = dict(body.provision) if body.provision is not None else {}
    # "Claiming binds it to your org, names it, AND ISSUES ITS MESH-NETWORK KEY. That's the
    # whole enrollment ceremony." (walkthrough J4 step 2). Until now claim only passed
    # through whatever an admin hand-made, so every bridge needed a key minted by hand -
    # the ceremony was three steps, not one. Mint it here when the fleet has mesh
    # configured and the caller did not supply one.
    if not prov.get("tailscale_auth_key"):
        from . import mesh
        try:
            minted = mesh.mint_ephemeral_key(
                "netbridge bridge %s" % (dev.pairing_code or device_id),
                tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
            if minted.get("key"):
                prov["tailscale_auth_key"] = minted["key"]
        except mesh.MeshNotConfigured:
            pass          # no TS credential on this fleet: claim still works, just no mesh
        except Exception as e:
            # Never fail a claim because the tailnet is unreachable - the device is claimed
            # either way and can be given a key later.
            _audit(db, actor, "claim:mesh-mint-failed", str(e)[:120])
    if prov:
        # Name the tailnet node too. Without this every bridge joins as "raspberrypi"
        # and Tailscale de-duplicates with -1/-2 suffixes, so a fleet of bridges is
        # unidentifiable on the mesh. Use the pairing code - the same identifier on the
        # label, in the SSID and in this panel.
        if prov.get("tailscale_auth_key") and not prov.get("tailscale_hostname"):
            code = (dev.pairing_code or "").replace("BRIDGE-", "").strip()
            if code:
                prov["tailscale_hostname"] = "netbridge-%s" % code
        dev.provision = prov
    db.commit()
    _audit(db, actor, "claim", "%s -> %s" % (dev.pairing_code or device_id, body.name))
    return _device_view(dev)


@app.delete("/admin/devices/{device_id}")
def forget_device(device_id: str, actor=Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    """Forget a device so it re-enrols as UNCLAIMED.

    There was no way to do this at all. Device identity comes from the Pi's CPU serial, so
    reflashing a card does NOT produce a new device — it re-enrols into the existing row and
    keeps whatever name and claim that row already had. Reassigning hardware to someone else,
    or testing the first-run flow, therefore had no supported path and meant editing the
    database by hand.

    The bridge recovers on its own: bridge-agent discards a token the fleet rejects and
    re-enrols with the bootstrap token, so it reappears within a poll cycle as
    "Unclaimed - just joined". No reboot, no reflash.

    This DELETES the device's history - telemetry, alerts, commands, diagnostics bundles -
    because a row that outlives its device is worse than no row: it attributes the last
    owner's incidents to the next one.
    """
    dev = db.get(Device, device_id)
    if not dev or dev.org_id != actor.org:
        raise HTTPException(404, "no such device")
    label = dev.name or dev.pairing_code or device_id
    from .models import Telemetry, AlertEvent, Command, DiagBundle
    removed = 0
    for model in (Telemetry, AlertEvent, Command, DiagBundle):
        try:
            removed += db.query(model).filter(model.device_id == device_id).delete(
                synchronize_session=False)
        except Exception:
            pass
    db.delete(dev)
    db.commit()
    _audit(db, actor, "device:forget", "%s (%d history rows)" % (label, removed))
    return {"ok": True, "forgot": label, "history_rows_removed": removed,
            "note": "the bridge will re-enrol as Unclaimed within a poll cycle"}


@app.post("/admin/devices/{device_id}/mesh-key")
def reissue_mesh_key(device_id: str, actor=Depends(auth.require_admin),
                     db: Session = Depends(get_db)):
    """Mint a FRESH mesh key for an already-claimed device.

    Claim issues the key once (walkthrough J4 step 2) and the device consumes it from
    /v1/provision exactly once. But a bridge's tailnet node is EPHEMERAL: if it stays
    offline long enough the control plane garbage-collects the node, and the device comes
    back with no mesh identity and no key waiting for it. Before this endpoint the only
    ways out were re-claiming it or editing the SD card by hand — so a bridge that merely
    went offline over a weekend needed physical surgery to rejoin the mesh.

    Re-keying is safe to repeat: keys are ephemeral, preauthorized and short-TTL, and the
    device simply picks up whichever one is waiting on its next poll (~15 s).
    """
    dev = _scoped_device(db, device_id, actor)
    from . import mesh
    try:
        minted = mesh.mint_ephemeral_key(
            "netbridge bridge %s" % (dev.pairing_code or device_id),
            tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
    except mesh.MeshNotConfigured:
        raise HTTPException(409, "this fleet has no tailnet credential configured")
    except Exception as e:
        raise HTTPException(502, "could not mint a mesh key: %s" % str(e)[:160])
    if not minted.get("key"):
        raise HTTPException(502, "tailnet returned no key")
    prov = dict(dev.provision or {})
    prov["tailscale_auth_key"] = minted["key"]
    code = (dev.pairing_code or "").replace("BRIDGE-", "").strip()
    if code:
        prov["tailscale_hostname"] = "netbridge-%s" % code
    dev.provision = prov
    db.commit()
    # Never log the key itself — only that one was issued, and to whom.
    _audit(db, actor, "mesh-key:reissue", dev.name or dev.pairing_code or device_id)
    return {"ok": True, "device": dev.name or device_id,
            "note": "key waiting — the bridge picks it up on its next check-in (~15 s)"}


@app.get("/admin/devices/{device_id}/label")
def device_label(device_id: str, actor=Depends(auth.require_admin),
                 db: Session = Depends(get_db)):
    """Everything needed to print (or REPRINT) a device's setup label — walkthrough J1
    step 3, build-ledger E1.

    The passphrase is random per device and lives on the device; a reflash wipes /data and
    the device generates+reports a NEW one. Without this endpoint that would silently
    invalidate a label already stuck on the box, with no way to recover it. Admin-only, and
    deliberately a separate call from the device views so the secret is never returned by a
    routine fleet listing."""
    dev = _scoped_device(db, device_id, actor)
    _audit(db, actor, "label:read", dev.name or dev.pairing_code or device_id)
    return {
        "device_id": dev.id,
        "pairing_code": dev.pairing_code,
        "name": dev.name,
        "ssid": "BridgeSetup-%s" % (dev.pairing_code or "").replace("BRIDGE-", ""),
        "password": dev.setup_pass,
        "note": ("device has not reported its passphrase yet"
                 if not dev.setup_pass else "reprintable - reflashing regenerates it"),
    }


@app.post("/admin/devices/{device_id}/pin")
def set_device_pin(device_id: str, body: dict | None = None,
                   actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """Set or rotate a bridge PIN (walkthrough J4: "Bridge PINs set and rotated here,
    delivered by you"). Generates a 6-digit PIN unless one is supplied, queues the
    set-pin command, and returns the PIN EXACTLY ONCE in this response. It is never
    emailed, never rendered in a device view, and its args are scrubbed from the command
    row as soon as the device confirms execution."""
    dev = _scoped_device(db, device_id, actor)
    pin = str((body or {}).get("pin") or "").strip()
    if pin:
        # 4-8, the same rule as the bridge (bridge-pin) and the agent: a 9-12 digit PIN used to
        # be accepted here, queued, and then refused on the bridge.
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise HTTPException(400, "pin must be 4-8 digits")
    else:
        import secrets as _s
        pin = "".join(_s.choice("0123456789") for _ in range(6))
    c = Command(device_id=device_id, type="set-pin", args={"pin": pin},
                timeout_s=_timeout_for("set-pin"))
    db.add(c)
    db.commit()
    # never audit the value itself - only that a rotation happened, and by whom
    _audit(db, actor, "pin:rotate", dev.name or device_id)
    out = {"command_id": c.id, "pin": pin, "note": "shown once - deliver it to the presenter offline"}
    if _pin_protocol(dev) < 2:
        out["warning"] = ("this bridge runs software from before 2026-09-25: it does not require the PIN on "
                          "every go-live, and it writes the PIN into its own log. Update the bridge, then "
                          "set the PIN again.")
    return out


# ---------------------------------------------------------------- script payloads
#
# WHY THE FLEET HOSTS THESE. deploy-script works by telling the BRIDGE to fetch a signed
# script from a URL — so that URL has to be somewhere the bridge can actually reach. Serving
# it from a laptop failed: a bridge on a venue network cannot route to it, the fetch hung,
# and the command died. But every bridge already talks to this control plane over public
# HTTPS every 15 seconds, from anywhere in the world. It was the obvious host all along.
#
# Unauthenticated on the read side, deliberately, exactly like the presenter-app updates
# above: the payload is protected by the EC signature the device verifies against a pinned
# key on its read-only root — twice, once at install and again at every service start. The
# scripts are not secrets (they ship inside the image), and a bridge fetching a fix must not
# need a credential to do it. Forging one requires the private signing key, which never
# leaves Samith's Mac.
PAYLOAD_DIR = os.environ.get("PAYLOAD_DIR", "/data/payloads")


# Names a bridge may be sent: the catalog on the device (/etc/netbridge/updatable.conf) is the
# real gate; this only keeps obvious junk out. Scripts / Python / tools start with "#!", the owner
# SSH key file is keys, "dropin.<unit>" is a systemd drop-in.
_PAYLOAD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_PAYLOAD_MAX = 2 * 1024 * 1024


def _payload_kind(name: str) -> str:
    if name == "owner_ssh_authorized_keys":
        return "keys"
    if name.startswith("dropin."):
        return "dropin"
    return "script"


@app.post("/admin/payloads")
async def upload_payload(request: Request, actor=Depends(auth.require_admin)):
    """Upload a signed file (script, Python, owner SSH keys, unit drop-in) + its detached
    signature, ready for a bridge to fetch."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not _PAYLOAD_NAME.fullmatch(name or "") or ".." in name or name.endswith(".sig"):
        raise HTTPException(400, "bad file name")
    # Only the shapes the bridges' catalog uses: *.sh, *.py, extension-less tools, the owner SSH
    # key file, and dropin.<unit>. Anything else - an .html above all - could be served back from
    # this origin as a page and read an admin's credentials (2026-09-25 audit).
    if not (name.endswith((".sh", ".py")) or "." not in name or name == "owner_ssh_authorized_keys"
            or re.fullmatch(r"dropin\.[a-z][a-z0-9-]{0,40}", name)):
        raise HTTPException(400, "not a catalog file name (*.sh, *.py, a tool name, owner_ssh_authorized_keys, dropin.<unit>)")
    script, sig = form.get("script"), form.get("sig")
    if script is None or sig is None:
        raise HTTPException(400, "need both 'script' and 'sig'")
    os.makedirs(PAYLOAD_DIR, exist_ok=True)
    body = await script.read()
    sigb = await sig.read()
    # Cheap sanity so a truncated upload cannot become a "deployable" payload. The real
    # verification is the device's, against its own pinned key — this only catches accidents.
    if len(sigb) < 32 or not body or len(body) > _PAYLOAD_MAX:
        raise HTTPException(400, "missing signature, empty, or larger than 2 MB")
    kind = _payload_kind(name)
    if kind == "script" and not body.startswith(b"#!"):
        raise HTTPException(400, "that does not look like a signed script (no #! line)")
    if kind == "keys" and b"ssh-" not in body:
        raise HTTPException(400, "that does not look like an SSH authorized_keys file")
    if kind == "dropin" and not re.search(rb"(?m)^\[(Unit|Service|Install)\]$", body):
        raise HTTPException(400, "that does not look like a systemd drop-in")
    with open(os.path.join(PAYLOAD_DIR, name), "wb") as f:
        f.write(body)
    with open(os.path.join(PAYLOAD_DIR, name + ".sig"), "wb") as f:
        f.write(sigb)
    _audit(next(get_db()), actor, "payload:upload", name)
    return {"name": name, "kind": kind, "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest()[:16],
            "url_base": (settings.public_base_url or "").rstrip("/") + "/payloads"}


@app.get("/admin/payloads")
def list_payloads(actor=Depends(auth.require_admin)):
    """What is currently available for a bridge to fetch."""
    try:
        names = sorted(n for n in os.listdir(PAYLOAD_DIR)
                       if not n.endswith(".sig") and os.path.isfile(os.path.join(PAYLOAD_DIR, n)))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        pth = os.path.join(PAYLOAD_DIR, n)
        with open(pth, "rb") as f:
            b = f.read()
        out.append({"name": n, "bytes": len(b),
                    "sha256": hashlib.sha256(b).hexdigest()[:16],
                    "signed": os.path.exists(pth + ".sig"),
                    "mtime": int(os.path.getmtime(pth))})
    return out


@app.get("/admin/payloads/ota")
def list_ota_payloads(actor=Depends(auth.require_admin)):
    """Whole-OS versions a bridge can be updated to (bridge-update.sh --version <v> fetches
    /payloads/ota/<v>/manifest.txt + .sig + the image it names). Published by
    tools/publish-ota.sh; the device verifies the signed manifest itself.

    Lists only versions a bridge can actually install (2026-09-28). publish-ota.sh used to copy
    straight into ota/<v>/, so for the minutes a 1.1 GB copy took the panel offered
    "Install on..." for a version whose image was missing or half written, and a bridge that
    fetched it failed on the hash. publish-ota.sh now stages in ota/.incoming-<v>/ and renames
    when complete; this is the second line: skip hidden (staging) directories, a missing or
    short image, and a manifest whose version or image name bridge-update.sh would refuse."""
    root = os.path.join(PAYLOAD_DIR, "ota")
    out = []
    try:
        versions = sorted(os.listdir(root))
    except FileNotFoundError:
        return out
    for v in versions:
        if v.startswith("."):
            continue                      # .incoming-<v> / .old-<v>: mid-publish or mid-removal
        mf = os.path.join(root, v, "manifest.txt")
        if not os.path.isfile(mf):
            continue
        kv = {}
        with open(mf) as f:
            for line in f:
                if "=" in line:
                    k, _, val = line.strip().partition("=")
                    kv[k] = val
        image = kv.get("image") or ""
        # bridge-update.sh fetches <fleet>/payloads/ota/<v>/ and refuses a manifest that is
        # incomplete, for another version, or names an image with a path in it - offering any of
        # those is offering a failure.
        if (kv.get("version") != v or not kv.get("sha256") or not image
                or "/" in image or image.startswith(".")):
            continue
        img = os.path.join(root, v, image)
        if not os.path.isfile(img):
            continue
        size = os.path.getsize(img)
        want = kv.get("size", "")
        if want.isdigit() and int(want) != size:
            continue                      # still arriving, or truncated
        out.append({"version": v, "image": image, "bytes": size,
                    "signed": os.path.exists(mf + ".sig"), "built": kv.get("built")})
    return out


@app.get("/admin/devices/{device_id}/commands")
def list_commands(device_id: str, limit: int = 20, actor=Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    """Recent commands for a device, WITH their output.

    The device has always posted results to /v1/commands/{id}/result and they have always
    been stored — but nothing served them back. "Fetch recent logs" queued a command whose
    output no operator could ever see; answering "did that action actually work?" meant
    opening the database over SSH. An action you cannot verify is an action you cannot trust.
    """
    dev = db.get(Device, device_id)
    if not dev or dev.org_id != actor.org:
        raise HTTPException(404, "no such device")
    from .models import Command
    # Expire before reporting, so an operator never reads a stale "sent" as "in progress".
    _sweep_expired(db)
    rows = db.scalars(select(Command).where(Command.device_id == device_id)
                      .order_by(desc(Command.id)).limit(max(1, min(limit, 100)))).all()
    return [{"id": c.id, "type": c.type, "status": c.status,
             "args": {} if c.type in PIN_BEARING_COMMANDS else c.args, "output": c.output,
             "created_at": c.created_at.isoformat() if c.created_at else None,
             "sent_at": c.sent_at.isoformat() if c.sent_at else None,
             "completed_at": c.completed_at.isoformat() if c.completed_at else None,
             "timeout_s": c.timeout_s,
             "fail_reason": c.fail_reason,
             # An operator asking "can I just try that again?" should not have to reason about
             # the state machine themselves.
             "cancellable": c.status == "pending",
             "retryable": c.status in ("failed", "rejected", "expired", "cancelled")}
            for c in rows]


@app.delete("/admin/devices/{device_id}/commands/{cmd_id}")
def cancel_command(device_id: str, cmd_id: int, actor=Depends(auth.require_admin),
                   db: Session = Depends(get_db)):
    """Cancel a command that has not yet reached the device.

    HONESTY IS THE WHOLE POINT OF THIS ENDPOINT
    -------------------------------------------
    A cancellation that only changes a row in the fleet database while the device goes ahead
    and executes the command anyway would be worse than having no cancel at all: an operator
    would believe the reboot was stopped, and the room would drop mid-meeting regardless.

    So this cancels exactly what can truly be cancelled, and refuses the rest with a reason:

      pending    the device has not collected it -> cancelled, guaranteed
      sent       the device already has it. Delivery is at-most-once and the agent executes
                 on the tick it collects, so there is no safe window to reach into. Refused
                 with 409 and told plainly, rather than pretending.
      terminal   done / failed / rejected / cancelled / expired are immutable history

    The window this protects is real but short - the agent polls every 15s - which is why the
    confirmation gate on destructive commands matters more than this endpoint does. Preventing
    the mistake beats recalling it.
    """
    dev = _scoped_device(db, device_id, actor)
    c = db.get(Command, cmd_id)
    if not c or c.device_id != device_id:
        raise HTTPException(404, "no such command for this device")
    if c.status == "pending":
        c.status = "cancelled"
        _scrub_secret_args(c)
        c.fail_reason = "cancelled by %s before the device collected it" % (
            getattr(actor, "email", None) or "an operator")
        c.completed_at = utcnow()
        db.commit()
        _audit(db, actor, "command:cancel:%s" % c.type, dev.name or device_id)
        return {"id": c.id, "status": c.status, "cancelled": True}
    if c.status == "sent":
        raise HTTPException(409, {
            "error": "already delivered",
            "status": c.status,
            "detail": ("the device collected this command and delivery is at-most-once, so it "
                       "cannot be recalled. It will reach a terminal state on its own within "
                       "%ds." % (c.timeout_s or DEFAULT_TIMEOUT_S)),
        })
    raise HTTPException(409, {
        "error": "already finished",
        "status": c.status,
        "detail": "completed commands are immutable history",
    })


@app.post("/admin/devices/{device_id}/commands")
def issue_command(device_id: str, body: IssueCommandIn, actor=Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    if body.type not in ALLOWED_COMMANDS:
        raise HTTPException(400, "unsupported command type")
    _refuse_by_policy(body)
    dev = _scoped_device(db, device_id, actor)
    _refuse_for_old_software(dev, body.type)
    # A destructive command must be asked for on purpose. The panel already shows a warning,
    # but a warning in a browser is not a control: the API is reachable without it, which is
    # precisely how a `reboot` reached a live bridge during the 2026-08-24 audit. Requiring
    # the caller to say so explicitly makes the accident impossible rather than regrettable.
    if body.type in CONFIRM_REQUIRED and not getattr(body, "confirm", False):
        raise HTTPException(400, {
            "error": "confirmation required",
            "type": body.type,
            "detail": ("this command interrupts service or changes what code runs; "
                       "re-issue it with confirm=true"),
        })
    # RETRY SAFETY.
    #
    # Nothing here used to prevent the same command being queued twice. A POST that timed out
    # in the client, a double-clicked button, or a proxy retry produced a SECOND row, and the
    # agent executed both. For `reboot` that is two reboots; for `deploy-script` it is the
    # same code deployed twice; for `golden-restore` it is a second restore over the first.
    # The confirm gate does not help -- a retry carries confirm=true as faithfully as the
    # original.
    #
    # Two layers, because they cover different callers:
    #
    #  1. An explicit idempotency_key, when the caller supplies one. Same key + same device
    #     returns the ORIGINAL command, whatever its state.
    #  2. An in-flight guard for commands that must not double-execute: if an identical one is
    #     already pending or sent for this device, hand back that one instead of queuing
    #     another. This needs no client change, which is what makes it actually protective.
    #
    # Read-only commands (diagnose, running, logs, read-file, *-show) are deliberately NOT
    # deduplicated: asking twice is harmless and an operator refreshing diagnostics should get
    # a fresh answer, not a stale row.
    if body.idempotency_key:
        prior = db.scalars(
            select(Command).where(Command.device_id == device_id,
                                  Command.idempotency_key == body.idempotency_key)
            .order_by(Command.id.desc()).limit(1)).first()
        if prior is not None:
            return {"id": prior.id, "status": prior.status, "timeout_s": prior.timeout_s,
                    "deduplicated": "idempotency_key"}

    if body.type in NO_DOUBLE_EXECUTE:
        inflight = db.scalars(
            select(Command).where(Command.device_id == device_id,
                                  Command.type == body.type,
                                  Command.status.in_(("pending", "sent")))
            .order_by(Command.id.desc()).limit(1)).first()
        if inflight is not None:
            return {"id": inflight.id, "status": inflight.status,
                    "timeout_s": inflight.timeout_s, "deduplicated": "already_in_flight"}

    c = Command(device_id=device_id, type=body.type, args=body.args or {},
                timeout_s=_timeout_for(body.type),
                idempotency_key=body.idempotency_key)
    db.add(c)
    db.commit()
    # audit: never include args (set-pin/unlock carry the PIN)
    _audit(db, actor, "command:%s" % body.type, dev.name or device_id)
    return {"id": c.id, "status": c.status, "timeout_s": c.timeout_s}


@app.post("/admin/commands/broadcast")
def broadcast_command(body: IssueCommandIn, actor=Depends(auth.require_admin),
                      db: Session = Depends(get_db)):
    """Queue the same command for every device IN THE CALLER'S ORG."""
    if body.type not in ALLOWED_COMMANDS:
        raise HTTPException(400, "unsupported command type")
    _refuse_by_policy(body)
    # THE CONFIRM GATE APPLIES HERE TOO.
    #
    # issue_command has required explicit confirmation for destructive types since the audit
    # in which an unconfirmed `reboot` reached a live bridge. Broadcast checked ALLOWED_COMMANDS
    # and nothing else -- so the single-device path refused an unconfirmed reboot while the
    # path that reboots EVERY DEVICE IN THE ORG accepted it. The stricter gate was on the
    # smaller blast radius.
    if body.type in CONFIRM_REQUIRED and not getattr(body, "confirm", False):
        raise HTTPException(400, {
            "error": "confirmation required",
            "type": body.type,
            "detail": ("this command interrupts service on EVERY device in the org; "
                       "re-issue it with confirm=true"),
        })
    ids, skipped = [], []
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        if body.type in OLD_SOFTWARE_REFUSALS and _pin_protocol(dev) < 2:
            skipped.append({"device": dev.name or dev.id, "reason": OLD_SOFTWARE_REFUSALS[body.type]})
            continue
        # timeout_s was omitted here, so broadcast commands fell back to the column default
        # instead of the per-class table the single-device path uses.
        c = Command(device_id=dev.id, type=body.type, args=body.args or {},
                    timeout_s=_timeout_for(body.type))
        db.add(c)
        db.flush()
        ids.append({"device": dev.name or dev.id, "command_id": c.id})
    db.commit()
    _audit(db, actor, "broadcast:%s" % body.type, "%d device(s)" % len(ids))
    return {"queued": ids, "skipped": skipped}


@app.get("/admin/alerts")
def all_alerts(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    out = []
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        for a in _safe_alerts(dev):
            out.append({"device_id": dev.id, "name": dev.name, **a})
    return out


@app.get("/admin/devices/{device_id}/uptime")
def device_uptime(device_id: str, actor=Depends(auth.require_admin),
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

    # EMAIL THE INVITE.
    #
    # This endpoint used to create the account, return the code once, and send nothing —
    # "delivered offline (like PINs)". Defensible for a PIN you read out over a phone, wrong
    # for an invite: the panel then showed "invite pending", which every admin reads as
    # "the email is on its way". On 2026-08-11 an admin invited a colleague, saw that label,
    # and waited for mail that was never going to arrive. Nothing failed; nothing was sent.
    #
    # SMTP is already configured here — the magic-link path uses it. Send synchronously,
    # because unlike sign-in there is no account-enumeration concern (the caller is an
    # authenticated admin who just created this account and already knows it exists), and
    # the admin needs to know NOW whether to deliver the code by hand.
    base = (settings.public_base_url or "").rstrip("/")
    lines = ["You have been added to NetBridge as %s." % role, ""]
    if base:
        lines += ["Open %s and paste this invite code:" % base, ""]
    else:
        lines += ["Open the NetBridge fleet page and paste this invite code:", ""]
    lines += ["    %s" % invite, "",
              "It can be used once. After that you sign in with a link sent to this address.",
              "If you were not expecting this, ignore the email."]
    emailed, mail_error = False, None
    try:
        emailed = notifier.send_mail(email, "You have been added to NetBridge",
                                     "\n".join(lines))
        if not emailed:
            mail_error = "SMTP is not configured on the fleet"
    except Exception as e:
        mail_error = "%s: %s" % (type(e).__name__, e)
        print("[invite] send failed for %s — %s" % (email, mail_error), flush=True)

    # The code is ALWAYS returned, emailed or not. If mail is down the admin must still be
    # able to deliver it by hand, and must be told that is now their job.
    return {"email": email, "role": role, "invite": invite,
            "emailed": emailed, "mail_error": mail_error}


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
    # Record this sign-in as its own session rather than overwriting the user's only
    # token — signing in here must not sign you out of the panel or the presenter app.
    from .models import Session as _S
    db.add(_S(user_id=u.id, token_hash=auth.hash_token(token), label="sign-in"))
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
            ok = notifier.send_mail(email, "Your NetBridge sign-in link", "\n".join(lines))
            if not ok:
                print("[signin] SMTP not configured — no link sent; the code is still valid "
                      "and can be pasted into the app", flush=True)
        except Exception as e:
            # LOG it. Swallowing this silently meant a user who never received a link had
            # no way to tell "the mail failed" from "the address has no account" - and
            # neither did we. The response stays generic (no account enumeration); only
            # the server log learns anything.
            print("[signin] send failed for a requested link: %s: %s"
                  % (type(e).__name__, e), flush=True)
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
    # Record this sign-in as its own session rather than overwriting the user's only
    # token — signing in here must not sign you out of the panel or the presenter app.
    from .models import Session as _S
    db.add(_S(user_id=u.id, token_hash=auth.hash_token(token), label="sign-in"))
    u.login_hash = None                     # single use
    u.login_expires = None
    db.commit()
    _audit(db, auth.Actor(u.email, u.org_id, u.role), "user:magic-signin")
    return {"email": u.email, "role": u.role, "org": u.org_id, "token": token}


@app.post("/auth/mesh-key")
def issue_mesh_key(actor=Depends(auth.require_viewer), db: Session = Depends(get_db)):
    """Phase 5: hand the presenter app a SCOPED ephemeral mesh (Tailscale) auth key
    so it joins the private network ITSELF — no admin key, no user-installed
    Tailscale, no hardcoded 100.x IP (walkthrough J3: "Joins the private mesh with
    an embedded client + scoped token from sign-in"). Any signed-in user (presenter
    or admin) may call it. The key is tagged tag:nb-source (the tailnet ACL grants
    that tag the bridges only), ephemeral (auto-removed on disconnect) and
    short-lived — so a leaked key reaches bridges, never other nodes, and dies fast.
    Also returns the caller's org bridges by tailnet name so the app connects by
    name via MagicDNS instead of a hardcoded IP."""
    import re as _re
    from . import mesh
    try:
        minted = mesh.mint_ephemeral_key("netbridge-source %s (%s)" % (actor.email, actor.org))
    except mesh.MeshNotConfigured:
        raise HTTPException(503, "mesh sign-in is not configured on this control plane")
    except mesh.MeshError as e:
        raise HTTPException(502, "could not mint a mesh key: %s" % e)
    tailnet = settings.ts_tailnet
    bridges = []
    for d in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        name = d.hostname or d.id
        bridges.append({
            "id": d.id,
            "name": name,
            "tailnet_name": ("%s.%s" % (name, tailnet)) if tailnet else name,
            "ip": d.tailscale_ip or "",
            "online": is_online(d),
        })
    # A stable, DNS-safe hostname for the app's ephemeral mesh node.
    slug = _re.sub(r"[^a-z0-9-]+", "-", actor.email.lower()).strip("-")[:40] or "user"
    _audit(db, actor, "auth:mesh-key")
    return {
        "authkey": minted["key"],
        "login_server": settings.ts_login_server,
        "tailnet": tailnet,
        "tag": settings.ts_source_tag,
        "hostname": "nb-source-" + slug,
        "expires": minted["expires"],
        "bridges": bridges,
    }


@app.get("/auth/bridges")
def presenter_bridges(actor=Depends(auth.require_viewer), db: Session = Depends(get_db)):
    """What the presenter app needs to go live — and nothing more.

    Any signed-in user (a presenter, or an admin who is presenting) gets the org's CLAIMED
    bridges with just enough to route to them. Live telemetry, alerts, commands and history stay
    admin-only under /admin/*."""
    out = []
    for d in db.scalars(select(Device).where(Device.org_id == actor.org,
                                             Device.claimed_at.is_not(None))).all():
        out.append({"id": d.id, "number": d.number, "label": fleet_label(d.number),
                    "name": d.name or d.pairing_code, "pairing_code": d.pairing_code,
                    "online": is_online(d), "tailscale_ip": d.tailscale_ip,
                    "ip": (d.latest or {}).get("ip")})
    out.sort(key=lambda b: (b["number"] is None, b["number"] or 0, b["name"] or ""))
    return out


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
    # db.scalars(select(Device.id)) already yields the IDs themselves, not Device rows.
    # Iterating them as objects and reading .id raised
    #     AttributeError: 'str' object has no attribute 'id'
    # on every call, so this endpoint had been returning 500 for its entire life and the
    # panel's alert history was permanently empty — including the flapped-overnight case
    # this function exists to show.
    org_ids = list(db.scalars(select(Device.id).where(Device.org_id == actor.org)).all())
    if not org_ids:
        return []
    rows = db.scalars(select(AlertEvent).where(AlertEvent.device_id.in_(org_ids))
                      .order_by(desc(AlertEvent.opened_at)).limit(50)).all()
    return [{"device_id": e.device_id, "kind": e.kind, "detail": e.detail,
             "opened_at": e.opened_at.isoformat() if e.opened_at else None,
             "notified": e.notified_at is not None,
             "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None}
            for e in rows]


# ----------------------------- alerts, bridge by bridge -----------------------------
# The Alerts page was one flat list: what is firing now, plus the newest 50 episodes for the
# whole fleet. One flapping bridge pushed every other bridge's history off the end within a day,
# and there was no way to ask "what has NB-002 been doing this week?" (owner, 2026-09-25).
#   /admin/alerts/bridges   which bridge needs me: one row per bridge, worst first
#   /admin/alerts/episodes  the full history, a page at a time, by bridge / state / severity / kind
# The live stream still pushes each episode as it opens or resolves; the panel uses those as the
# signal to refresh the page it is showing.
INFO_KINDS = frozenset(("new_device",))          # a record, not a problem


def _alert_severity(kind: str) -> str:
    return "info" if kind in INFO_KINDS else notifier._severity(kind)


def _utc(t):
    """SQLite hands back tz-naive datetimes; they are UTC (same rule as alerts.is_online)."""
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


def _iso(t):
    return _utc(t).isoformat() if t is not None else None


def _bridge_name(d: Device) -> str:
    return " · ".join(x for x in (fleet_label(d.number), d.name or d.pairing_code or d.id) if x)


@app.get("/admin/alerts/bridges")
def alerts_by_bridge(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """One row per bridge: what is firing on it now (with its one-click fix and since when), and
    how often it alerted in the last 24 h / 7 days. Bridges with critical alerts first, then
    warnings, then the rest in fleet-number order."""
    from sqlalchemy import func
    from .models import AlertEvent
    devs = db.scalars(select(Device).where(Device.org_id == actor.org)).all()
    ids = [d.id for d in devs]
    now = utcnow()
    since, day, week, last = {}, {}, {}, {}
    if ids:
        for e in db.scalars(select(AlertEvent).where(AlertEvent.device_id.in_(ids),
                                                     AlertEvent.resolved_at.is_(None))).all():
            k = (e.device_id, e.kind)
            if k not in since or _utc(e.opened_at) < since[k]:
                since[k] = _utc(e.opened_at)
        for window, into in ((dt.timedelta(hours=24), day), (dt.timedelta(days=7), week)):
            for dev_id, n in db.execute(select(AlertEvent.device_id, func.count())
                                        .where(AlertEvent.device_id.in_(ids), AlertEvent.opened_at >= now - window,
                                               AlertEvent.kind.not_in(tuple(INFO_KINDS)))
                                        .group_by(AlertEvent.device_id)).all():
                into[dev_id] = n
        for dev_id, t in db.execute(select(AlertEvent.device_id, func.max(AlertEvent.opened_at))
                                    .where(AlertEvent.device_id.in_(ids), AlertEvent.kind.not_in(tuple(INFO_KINDS)))
                                    .group_by(AlertEvent.device_id)).all():
            last[dev_id] = t
    rows = []
    for d in devs:
        online = is_online(d)
        alerts = _safe_alerts(d)
        firing = [{"kind": a["kind"], "title": notifier.alert_title(a["kind"]), "severity": _alert_severity(a["kind"]),
                   "detail": a.get("detail"),
                   "fix": a.get("fix"), "since": _iso(since.get((d.id, a["kind"])))} for a in alerts]
        crit = sum(1 for a in firing if a["severity"] == "critical")
        rows.append({"device_id": d.id, "number": d.number, "label": fleet_label(d.number), "name": d.name,
                     "pairing_code": d.pairing_code, "claimed": d.claimed_at is not None, "online": online,
                     "state": _device_state(d, online, alerts), "open": firing,
                     "open_critical": crit, "open_warning": len(firing) - crit,
                     "episodes_24h": day.get(d.id, 0), "episodes_7d": week.get(d.id, 0),
                     "last_alert_at": _iso(last.get(d.id))})
    rows.sort(key=lambda r: (-r["open_critical"], -r["open_warning"], r["number"] is None, r["number"] or 0,
                             (r["name"] or r["pairing_code"] or r["device_id"]).lower()))
    return {"bridges": rows,
            "totals": {"bridges": len(rows),
                       "needing_attention": sum(1 for r in rows if r["open"]),
                       "open_critical": sum(r["open_critical"] for r in rows),
                       "open_warning": sum(r["open_warning"] for r in rows),
                       "episodes_24h": sum(r["episodes_24h"] for r in rows)}}


@app.get("/admin/alerts/episodes")
def alert_episodes(device_id: str | None = Query(None, max_length=128),
                   status: str = Query("all", pattern="^(all|open|resolved)$"),
                   severity: str = Query("all", pattern="^(all|critical|warning|info)$"),
                   kind: str | None = Query(None, pattern="^[a-z_]{1,40}$"),
                   page: int = Query(1, ge=1, le=1_000_000),
                   page_size: int = Query(25, ge=1, le=100),
                   actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """Alert episodes (fired -> resolved), newest first, one page at a time. Filters: one bridge,
    open / resolved, severity, kind. A page past the end returns the LAST page (after a bridge is
    forgotten, a panel that was on page 9 of 9 lands on 8 of 8, not on an empty table)."""
    from sqlalchemy import func
    from .models import AlertEvent
    devs = {d.id: d for d in db.scalars(select(Device).where(Device.org_id == actor.org)).all()}
    if device_id is not None and device_id not in devs:
        raise HTTPException(404, "no such bridge")        # another org's bridge looks exactly like no bridge
    base = [AlertEvent.device_id == device_id] if device_id is not None else \
        [AlertEvent.device_id.in_(select(Device.id).where(Device.org_id == actor.org))]
    if kind is not None:
        base.append(AlertEvent.kind == kind)
    if severity == "critical":
        base.append(AlertEvent.kind.in_(tuple(notifier.CRITICAL_KINDS)))
    elif severity == "info":
        base.append(AlertEvent.kind.in_(tuple(INFO_KINDS)))
    elif severity == "warning":
        base.append(AlertEvent.kind.not_in(tuple(notifier.CRITICAL_KINDS | INFO_KINDS)))
    count = lambda *extra: db.scalar(select(func.count(AlertEvent.id)).where(*base, *extra)) or 0
    n_open, n_resolved = count(AlertEvent.resolved_at.is_(None)), count(AlertEvent.resolved_at.is_not(None))
    wanted = {"all": [], "open": [AlertEvent.resolved_at.is_(None)], "resolved": [AlertEvent.resolved_at.is_not(None)]}[status]
    total = {"all": n_open + n_resolved, "open": n_open, "resolved": n_resolved}[status]
    pages = max(1, -(-total // page_size))
    page = min(page, pages)
    rows = db.scalars(select(AlertEvent).where(*base, *wanted)
                      .order_by(desc(AlertEvent.opened_at), desc(AlertEvent.id))
                      .offset((page - 1) * page_size).limit(page_size)).all()
    now = utcnow()
    items = []
    for e in rows:
        opened, resolved = _utc(e.opened_at), _utc(e.resolved_at)
        d = devs.get(e.device_id)
        items.append({"id": e.id, "device_id": e.device_id, "device": _bridge_name(d) if d else e.device_id,
                      "kind": e.kind, "title": notifier.alert_title(e.kind), "severity": _alert_severity(e.kind),
                      "detail": e.detail,
                      "opened_at": _iso(opened), "resolved_at": _iso(resolved),
                      "duration_s": int(((resolved or now) - opened).total_seconds()) if opened else None,
                      "notified": e.notified_at is not None})
    return {"items": items, "page": page, "page_size": page_size, "pages": pages, "total": total,
            "has_prev": page > 1, "has_next": page < pages,
            "first": (page - 1) * page_size + 1 if items else 0, "last": (page - 1) * page_size + len(items),
            "counts": {"open": n_open, "resolved": n_resolved, "all": n_open + n_resolved}}


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


# ----------------------------- live stream for the panel -----------------------------
# The panel used to poll every 5 s and rebuild whole sections of the page, which wiped whatever
# the operator was typing and made buttons jump. The server now PUSHES what changed — Server-Sent
# Events over an ordinary authenticated GET, checked about once a second — and the page updates
# values in place. Deltas only: a quiet fleet sends a keep-alive every 15 s and nothing else.
STREAM_TICK_S = 1.0


def _command_view(c, device_name=None) -> dict:
    out = c.output or ""
    return {"id": c.id, "device_id": c.device_id, "device": device_name, "type": c.type,
            "status": c.status,
            # never ship a PIN to a browser, not even an admin's
            "args": {} if c.type in PIN_BEARING_COMMANDS else (c.args or {}),
            "output": out[-4000:], "output_truncated": len(out) > 4000,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "sent_at": c.sent_at.isoformat() if c.sent_at else None,
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
            "timeout_s": c.timeout_s, "fail_reason": c.fail_reason,
            "cancellable": c.status == "pending",
            "retryable": c.status in ("failed", "rejected", "expired", "cancelled")}


@app.get("/admin/stream")
async def admin_stream(request: Request, actor=Depends(auth.require_admin)):
    """Everything the panel shows, pushed as it changes: bridges (with state and alerts),
    commands (with their progress and output) and alert episodes (fired / resolved)."""
    import asyncio
    from .db import SessionLocal
    from .models import AlertEvent
    org = actor.org
    token = (request.headers.get("authorization") or "").split(" ", 1)[-1].strip()

    def snapshot():
        db = SessionLocal()
        try:
            _sweep_expired(db)
            devs = db.scalars(select(Device).where(Device.org_id == org)).all()
            names = {d.id: " ".join(x for x in (fleet_label(d.number), d.name or d.pairing_code or d.id) if x)
                     for d in devs}
            dviews = {d.id: _device_view(d) for d in devs}
            ids = list(dviews)
            cviews, aviews = {}, {}
            if ids:
                for c in db.scalars(select(Command).where(Command.device_id.in_(ids))
                                    .order_by(desc(Command.id)).limit(80)).all():
                    cviews[c.id] = _command_view(c, names.get(c.device_id))
                for e in db.scalars(select(AlertEvent).where(AlertEvent.device_id.in_(ids))
                                    .order_by(desc(AlertEvent.id)).limit(60)).all():
                    aviews[e.id] = {"id": e.id, "device_id": e.device_id, "device": names.get(e.device_id),
                                    "kind": e.kind, "title": notifier.alert_title(e.kind),
                                    "severity": _alert_severity(e.kind), "detail": e.detail,
                                    "opened_at": e.opened_at.isoformat() if e.opened_at else None,
                                    "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
                                    "notified": e.notified_at is not None}
            return dviews, cviews, aviews
        finally:
            db.close()

    def still_admin():
        db = SessionLocal()
        try:
            try:
                return auth.require_admin(authorization="Bearer " + token, db=db).org == org
            except HTTPException:
                return False
        finally:
            db.close()

    def fingerprint(v):
        return hashlib.sha1(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()

    async def events():
        seen = {"device": {}, "command": {}, "alert": {}}
        last_beat = last_auth = time.monotonic()
        first = True
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            if time.monotonic() - last_auth >= 60:           # a revoked admin stops receiving
                last_auth = time.monotonic()
                if not await asyncio.to_thread(still_admin):
                    yield "event: revoked\ndata: {}\n\n"
                    return
            try:
                dviews, cviews, aviews = await asyncio.to_thread(snapshot)
            except Exception:
                await asyncio.sleep(STREAM_TICK_S)
                continue
            out = []
            if first:
                out.append(("hello", {"server_time": utcnow().isoformat(), "who": actor.email,
                                      "org": org, "tick_s": STREAM_TICK_S}))
            for kind, views in (("device", dviews), ("command", cviews), ("alert", aviews)):
                s = seen[kind]
                for k, v in views.items():
                    f = fingerprint(v)
                    if s.get(k) != f:
                        s[k] = f
                        out.append((kind, v))
                if kind == "device":
                    for k in [k for k in s if k not in views]:
                        del s[k]
                        out.append(("device_removed", {"id": k}))
            if first:
                out.append(("ready", {"devices": len(dviews)}))
                first = False
            for kind, data in out:
                yield "event: %s\ndata: %s\n\n" % (kind, json.dumps(data, default=str))
            now = time.monotonic()
            if now - last_beat >= 15:
                last_beat = now
                yield ": keep-alive\n\n"
            await asyncio.sleep(STREAM_TICK_S)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform",
                                      "X-Accel-Buffering": "no"})


# ----------------------------- admin panel (static) -----------------------------
# Serve the built React panel from the same process.
#
# CRITICAL ORDERING: a StaticFiles mount at "/" matches EVERY path, and Starlette
# tries routes in registration order. So this mount must be registered AFTER every
# API route, or it silently shadows the ones declared below it — the exact bug that
# made staged rollouts (declared further down) return 405/404 and look "never wired"
# when the panel was present. It is therefore defined here but CALLED at the very
# bottom of the module, once all @app routes exist. Do not app.mount() inline here.
import os as _os
from fastapi.staticfiles import StaticFiles


def _mount_app_updates():
    """Publish presenter-app updates (walkthrough J3 "kept current by auto-update").

    Serves <APP_RELEASE_DIR>/<platform>/{manifest.txt,manifest.txt.sig,<binary>}. Static
    and unauthenticated ON PURPOSE: the payload is protected by the EC signature the app
    verifies against a pinned key, not by who can fetch it — and an app too old to hold a
    valid token still has to be able to update itself. Must be mounted BEFORE the "/"
    panel mount for the same route-shadowing reason described above.
    """
    d = _os.environ.get("APP_RELEASE_DIR") or _os.path.join(
        _os.path.dirname(__file__), "..", "..", "..", "app", "netbridge-source", "dist", "release")
    if _os.path.isdir(d):
        d = _os.path.abspath(d)
        print("[app-update] serving presenter-app updates from %s" % d)
        app.mount("/app", StaticFiles(directory=d), name="app-updates")
    else:
        print("[app-update] no release dir (%s) — auto-update is inert until a build "
              "publishes one" % d)


def _mount_payloads():
    """Serve /payloads/<name> and <name>.sig for a bridge to fetch. Same reasoning as the
    app-update mount: static, unauthenticated, signature-gated."""
    d = PAYLOAD_DIR
    os.makedirs(d, exist_ok=True)
    print("[payloads] serving signed script payloads from %s" % d)
    class _DownloadOnly(StaticFiles):
        """Every payload is a download: never rendered, never sniffed, never scripted."""
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Content-Type"] = "application/octet-stream"
            resp.headers["Content-Disposition"] = "attachment"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
            return resp
    app.mount("/payloads", _DownloadOnly(directory=d), name="payloads")


def _mount_panel():
    # The built panel has lived at control-plane/panel-dist, but an earlier version only
    # looked for backend/static — and os.path.isdir() failing just SKIPS the mount, so "/"
    # answered 404 with no error anywhere. Check the real locations, log which one won, and
    # say so loudly when none match.
    _here = _os.path.dirname(__file__)
    for _cand in (_os.path.join(_here, "..", "..", "panel-dist"),   # control-plane/panel-dist
                  _os.path.join(_here, "..", "static"),             # backend/static
                  _os.path.join(_here, "..", "panel-dist")):
        if _os.path.isdir(_cand):
            static = _os.path.abspath(_cand)
            print("[panel] serving admin UI from %s" % static)

            class _NoCacheStatic(StaticFiles):
                """Serve the panel with no-store.

                StaticFiles' default validators let a browser keep showing an OLD index.html
                after the server has been updated. That is not a cosmetic problem here: a
                sign-in fix shipped, the server had it, and the browser kept rendering the
                broken page and its old error text - indistinguishable from "the fix didn't
                work", with no way for the user to tell. The panel is one small HTML file
                served from localhost or a tailnet; revalidating it every time costs nothing
                next to shipping a fix nobody can see."""
                async def get_response(self, path, scope):
                    resp = await super().get_response(path, scope)
                    resp.headers["Cache-Control"] = "no-store, must-revalidate"
                    resp.headers["Pragma"] = "no-cache"
                    return resp

            app.mount("/", _NoCacheStatic(directory=static, html=True), name="panel")
            return
    print("[panel] NO admin UI found — '/' will 404. Looked for panel-dist / static "
          "next to app/. Build the panel or check the checkout.")


# ----------------------------- staged rollouts (walkthrough J4) -----------------------------
# "A/B image update, staged 10% -> 100% · 22 of 25 updated · 0 rollbacks ·
#  SF Lab queued until online."
#
# Division of labour: the DEVICE already owns the risky half — bridge-update.sh
# verifies the signed manifest, writes the standby slot and tryboots it, and the
# on-device health check auto-commits or auto-rolls-back. So a failed update is
# already safe by the time we hear about it. What lives here is only the fleet
# question: who gets it, in what wave, and whether it is safe to widen.

ROLLOUT_STAGES = [10, 25, 50, 100]


def _next_stage(pct: int) -> int | None:
    for s in ROLLOUT_STAGES:
        if s > pct:
            return s
    return None


def _ro_targets(db: Session, ro: Rollout) -> list[RolloutTarget]:
    # Deterministic order so waves are reproducible and an operator can predict
    # which devices go first (never random, never "whoever polled last").
    return db.scalars(
        select(RolloutTarget).where(RolloutTarget.rollout_id == ro.id)
        .order_by(RolloutTarget.device_id)
    ).all()


def _sync_rollout(db: Session, ro: Rollout) -> None:
    """Fold finished command results back into target state.

    Derived on read rather than hooked into /v1/commands/{id}/result, so the hot
    device-facing path stays untouched (and a rollout can never slow it down).
    """
    changed = False
    for t in _ro_targets(db, ro):
        if t.status != "dispatched" or not t.command_id:
            continue
        c = db.get(Command, t.command_id)
        if not c:
            continue
        if c.status == "done":
            t.status, t.updated_at, changed = "succeeded", utcnow(), True
        elif c.status in ("failed", "rejected", "expired"):
            # The device already rolled itself back into the previous slot - or it never
            # reported back at all (expired), which must halt widening just the same rather
            # than leave the target "updating" forever.
            t.status, t.updated_at, changed = "failed", utcnow(), True
        elif c.status == "cancelled":
            # An admin recalled it before the bridge collected it: send it again next wave.
            t.status, t.command_id, t.updated_at, changed = "queued", None, utcnow(), True
    if changed:
        db.commit()


def _dispatch_rollout(db: Session, ro: Rollout) -> int:
    """Queue `update` commands up to the current wave, ONLINE devices only.

    Offline devices are deliberately left `queued` (not skipped, not failed) —
    they pick the update up on a later dispatch once they are back.
    """
    if ro.status != "active":
        return 0
    targets = _ro_targets(db, ro)
    total = len(targets)
    if not total:
        return 0
    # ceil() so a 10% wave over a small fleet still moves at least one device.
    allowed = max(1, -(-total * ro.stage_pct // 100))
    started = sum(1 for t in targets if t.status != "queued")
    room = allowed - started
    sent = 0
    for t in targets:
        if room <= 0:
            break
        if t.status != "queued":
            continue
        dev = db.get(Device, t.device_id)
        if not dev or not is_online(dev):
            continue                      # "queued until online"
        # timeout_s: an OS update may take an hour over a venue uplink. Without it the command
        # took the column default (120 s), was marked EXPIRED mid-download, the bridge's later
        # "done" was refused, and the rollout sat "updating" forever and could never widen.
        c = Command(device_id=dev.id, type="update", args={"source": ro.source},
                    timeout_s=_timeout_for("update"))
        db.add(c)
        db.flush()
        t.command_id, t.status, t.wave, t.updated_at = c.id, "dispatched", ro.stage_pct, utcnow()
        room -= 1
        sent += 1
    if sent:
        ro.updated_at = utcnow()
        db.commit()
    return sent


def _rollout_view(db: Session, ro: Rollout) -> dict:
    targets = _ro_targets(db, ro)
    by = {"queued": 0, "dispatched": 0, "succeeded": 0, "failed": 0}
    waiting_offline = []
    for t in targets:
        by[t.status] = by.get(t.status, 0) + 1
        if t.status == "queued":
            dev = db.get(Device, t.device_id)
            if dev and not is_online(dev):
                waiting_offline.append(dev.name or dev.id)
    return {
        "id": ro.id, "version": ro.version, "source": ro.source,
        "status": ro.status, "stage_pct": ro.stage_pct,
        "next_stage": _next_stage(ro.stage_pct),
        "total": len(targets),
        "updated": by["succeeded"], "rollbacks": by["failed"],
        "in_flight": by["dispatched"], "queued": by["queued"],
        "queued_offline": waiting_offline,
        "created_by": ro.created_by,
        "created_at": ro.created_at.isoformat() if ro.created_at else None,
        # the walkthrough's one-liner, rendered server-side
        "summary": "%d of %d updated · %d rollback%s%s" % (
            by["succeeded"], len(targets), by["failed"],
            "" if by["failed"] == 1 else "s",
            " · %s queued until online" % ", ".join(waiting_offline) if waiting_offline else ""),
    }


@app.post("/admin/rollouts")
def create_rollout(body: RolloutCreateIn, actor=Depends(auth.require_admin),
                   db: Session = Depends(get_db)):
    """Open a staged rollout over the caller's org and dispatch the first wave."""
    if body.stage_pct not in ROLLOUT_STAGES:
        raise HTTPException(400, "stage_pct must be one of %s" % ROLLOUT_STAGES)
    if db.scalar(select(Rollout).where(Rollout.org_id == actor.org, Rollout.status == "active")):
        raise HTTPException(409, "an active rollout already exists for this org")
    ro = Rollout(org_id=actor.org, version=body.version, source=body.source,
                 stage_pct=body.stage_pct, created_by=getattr(actor, "email", "admin"))
    db.add(ro)
    db.flush()
    # Devices already on the target version are not targets — re-running a
    # rollout must never re-flash a device that is already there. Nor are unclaimed bridges, or
    # bridges older than 2.2: their software cannot install an OS version remotely (the update
    # is killed after 30 s, the target sits "updating" until it expires, and widening stalls) -
    # those are flashed by hand (review, 2026-09-25). The response names them.
    excluded, targets = [], 0
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        if (dev.version or "") == body.version:
            continue
        if dev.claimed_at is None or _pin_protocol(dev) < 2:
            excluded.append(dev.name or dev.pairing_code or dev.id)
            continue
        db.add(RolloutTarget(rollout_id=ro.id, device_id=dev.id))
        targets += 1
    if not targets:
        db.rollback()                     # an empty rollout would sit "active" doing nothing
        raise HTTPException(409, "no bridge can install %s remotely%s" % (
            body.version, (": %s run software older than 2.2 or are unclaimed - flash their cards "
                           "with the image instead" % ", ".join(excluded)) if excluded else
            " (they are all on it already)"))
    db.commit()
    sent = _dispatch_rollout(db, ro)
    _audit(db, actor, "rollout:create", "%s -> %d device(s), wave %d%%" %
           (body.version, len(_ro_targets(db, ro)), ro.stage_pct))
    out = _rollout_view(db, ro)
    out["dispatched_now"] = sent
    out["excluded"] = excluded
    return out


@app.get("/admin/rollouts")
def list_rollouts(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    ros = db.scalars(select(Rollout).where(Rollout.org_id == actor.org)
                     .order_by(desc(Rollout.created_at))).all()
    for ro in ros:
        _sync_rollout(db, ro)
    return [_rollout_view(db, ro) for ro in ros]


def _scoped_rollout(db: Session, rollout_id: int, actor) -> Rollout:
    ro = db.get(Rollout, rollout_id)
    if not ro or ro.org_id != actor.org:
        raise HTTPException(404, "rollout not found")
    return ro


@app.get("/admin/rollouts/{rollout_id}")
def get_rollout(rollout_id: int, actor=Depends(auth.require_admin),
                db: Session = Depends(get_db)):
    ro = _scoped_rollout(db, rollout_id, actor)
    _sync_rollout(db, ro)
    view = _rollout_view(db, ro)
    view["devices"] = [
        {"device_id": t.device_id, "status": t.status, "wave": t.wave,
         "command_id": t.command_id}
        for t in _ro_targets(db, ro)
    ]
    return view


@app.post("/admin/rollouts/{rollout_id}/dispatch")
def dispatch_rollout(rollout_id: int, actor=Depends(auth.require_admin),
                     db: Session = Depends(get_db)):
    """Catch up the current wave — picks up devices that have come back online."""
    ro = _scoped_rollout(db, rollout_id, actor)
    _sync_rollout(db, ro)
    sent = _dispatch_rollout(db, ro)
    out = _rollout_view(db, ro)
    out["dispatched_now"] = sent
    return out


@app.post("/admin/rollouts/{rollout_id}/advance")
def advance_rollout(rollout_id: int, force: bool = False,
                    actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """Widen to the next wave — refused while the current one is unhealthy.

    This is the "0 rollbacks" guard: any device that rolled itself back halts
    the fleet here instead of letting a bad image reach everyone. `force=true`
    is the deliberate operator override.
    """
    ro = _scoped_rollout(db, rollout_id, actor)
    if ro.status != "active":
        raise HTTPException(409, "rollout is %s" % ro.status)
    _sync_rollout(db, ro)
    view = _rollout_view(db, ro)
    if view["rollbacks"] and not force:
        raise HTTPException(409, "%d device(s) rolled back — halting; pass force=true to override"
                            % view["rollbacks"])
    if view["in_flight"] and not force:
        raise HTTPException(409, "%d device(s) still updating" % view["in_flight"])
    nxt = _next_stage(ro.stage_pct)
    if nxt is None:
        # Already at the widest wave: finish once nothing is left outstanding.
        if not view["queued"] and not view["in_flight"]:
            ro.status = "completed"
            ro.updated_at = utcnow()
            db.commit()
            _audit(db, actor, "rollout:complete", ro.version)
        done = _rollout_view(db, ro)
        done["dispatched_now"] = 0        # keep the response shape consistent
        return done
    ro.stage_pct, ro.updated_at = nxt, utcnow()
    db.commit()
    sent = _dispatch_rollout(db, ro)
    _audit(db, actor, "rollout:advance", "%s -> %d%%" % (ro.version, nxt))
    out = _rollout_view(db, ro)
    out["dispatched_now"] = sent
    return out


@app.post("/admin/rollouts/{rollout_id}/{action}")
def control_rollout(rollout_id: int, action: str, actor=Depends(auth.require_admin),
                    db: Session = Depends(get_db)):
    """pause | resume | abort. Abort stops further waves; it never un-does a
    device that already updated (that is what a new rollout is for)."""
    if action not in ("pause", "resume", "abort"):
        raise HTTPException(404, "unknown action")
    ro = _scoped_rollout(db, rollout_id, actor)
    ro.status = {"pause": "paused", "resume": "active", "abort": "aborted"}[action]
    ro.updated_at = utcnow()
    db.commit()
    _audit(db, actor, "rollout:%s" % action, ro.version)
    return _rollout_view(db, ro)


# Mount the static panel LAST — after every @app route above — so its catch-all "/"
# never shadows an API route (see _mount_panel's note). This must remain the final
# statement that touches `app`. /app (updates) goes first: it is a narrow prefix, but it
# still has to beat the "/" catch-all.
_mount_app_updates()
_mount_payloads()   # narrow prefix, before the "/" catch-all
_mount_panel()
