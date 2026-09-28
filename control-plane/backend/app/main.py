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
from sqlalchemy import select, desc, update
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from . import auth, models, notifier
from .alerts import (device_alerts, is_online, HISTORY_KINDS, history_alert, unreadable_alert, alert_fix,
                     bridge_title, REFUSED_WHILE_BUSY)
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
    # a whole image over whatever uplink the venue has, a wait for a live meeting to end, and the
    # slot write: the agent's 3-hour limit for the job (bridge-agent.py DETACHED, 10800) plus 10
    # minutes to report. One hour covered download AND write, and slow venues expired mid-write
    # (2026-09-28).
    "update": 11400,
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
#
# "Identical" means the same type AND the same args (2026-09-28). The guard used to match on the
# type alone, so `deploy bridge-agent.py` while `deploy bridge-web.py` was in flight got the
# bridge-web.py command back: nb printed "queued" and then "done", and bridge-agent.py never
# reached the bridge. The same swapped one PIN for another and one OS version for another. A
# DIFFERENT one of these while another is in flight is refused (409) rather than queued: the
# bridge runs each in its own background job with no lock between them, so two OS updates or
# two deploys would run at the same time.
NO_DOUBLE_EXECUTE = {"reboot", "restart", "update", "deploy-script", "revert-script",
                     "golden-restore", "golden-save", "unquarantine", "reset-clock",
                     "set-pin", "profile", "gadget-tune", "gadget-tune-clear"}

# A command in one of these has finished as far as the control plane is concerned. Nothing a
# device says afterwards may overwrite it -- see command_result(). `pending` and `sent` are the
# only states from which a result is accepted.
TERMINAL_STATES = models.COMMAND_TERMINAL_STATES   # one set, shared with the retention sweep

# The only results a device may report. Anything else is recorded as "failed" (see
# command_result): "pending" used to put the row back in the pull queue so it ran twice, and an
# unknown word left it neither finished nor running, with its rollout target "updating" forever.
DEVICE_RESULT_STATES = ("done", "failed", "rejected")

# Commands that interrupt a meeting the moment they run: media restarts (with the meeting laptop
# attached those have rebooted under-powered bridges) and a reboot. Two rules use this list
# (2026-09-28):
#   * one queued for a bridge that is offline goes STALE: it expires if the bridge has not
#     collected it within STALE_UNCOLLECTED_S. Queue-until-online stays for everything else, but
#     a reboot queued on a Friday ran on the Monday, on the bridge's first heartbeat - just as
#     someone plugged the laptop in for a meeting.
#   * a broadcast skips bridges with a meeting laptop attached or a presenter live.
# A lock is deliberately NOT here: ending live sessions is what it is for, and one that reaches its
# bridge late still does what the admin asked - keeps people out - so it neither goes stale nor
# skips a busy bridge. Expiring it would quietly leave a bridge open that an admin locked.
# A forced OS update counts too (_goes_stale): it skips the bridge's own "not during a meeting" check.
INTERRUPTS_MEETING = {"reboot", "restart", "start", "stop", "profile", "reset-clock",
                      "golden-restore", "jitter-fix", "jitter-reset"}
STALE_UNCOLLECTED_S = 30 * 60


def _goes_stale(ctype: str, args) -> bool:
    """True for a command that must not run long after it was asked for (INTERRUPTS_MEETING).
    An OS update refuses on the bridge while a meeting is on; one sent with force does not, so it
    goes stale like a reboot."""
    return ctype in INTERRUPTS_MEETING or (ctype == "update" and bool((args or {}).get("force")))


def _stale_reason(ctype: str) -> str:
    return ("never collected: the bridge did not pick it up within %d min of being asked, and a %s "
            "that runs long after it was asked for can land in the middle of a meeting. Send it "
            "again if it is still wanted." % (STALE_UNCOLLECTED_S // 60, ctype))


def _move(db: Session, c, frm, **values) -> bool:
    """Change a command's state only if it is still in one of `frm`, in ONE conditional UPDATE.

    Every state change used to be read -> check -> assign -> commit, and SQLite serialises the two
    writes without noticing that they conflict: a cancel could overwrite the "sent" the bridge's
    poll had just committed (the panel said "cancelled, never received" while the bridge ran it),
    and the sweeper could turn a "done" into "expired" (2026-09-28). With the WHERE the second
    writer sees that it lost. Returns whether this caller won; the caller commits."""
    if c.type in PIN_BEARING_COMMANDS and values.get("status") in TERMINAL_STATES:
        values["args"] = {"_scrubbed": True}          # a PIN never outlives delivery
    res = db.execute(update(Command).where(Command.id == c.id, Command.status.in_(tuple(frm)))
                     .values(**values).execution_options(synchronize_session=False))
    db.expire(c)                                      # re-read: the row is what the database says
    return res.rowcount == 1


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
    """Move commands that can no longer finish into a terminal state.

      sent     delivered and never answered within its class timeout
      pending  never collected, for a command that goes stale (_goes_stale): anything else
               still waits for its bridge to come back, which is not a fault

    Runs on a 30 s timer (_housekeeping_loop) AND on every path that reads or acts on command
    state - issuing a command, the device and command views, every rollout endpoint. Until
    2026-09-28 only the command list and the panel's stream swept, so with no panel open a reboot
    whose result was lost stayed "sent" for ever: the next reboot was "deduplicated" onto that
    dead row and never queued, and a rollout sat "updating" until somebody happened to look.
    """
    now = utcnow()
    n = 0
    for c in db.scalars(select(Command).where(Command.status.in_(("sent", "pending")))).all():
        if c.status == "sent":
            started, limit = c.sent_at or c.created_at, c.timeout_s or DEFAULT_TIMEOUT_S
            reason = ("no result within %ds of delivery — the device may have rebooted, "
                      "lost its uplink, or died mid-command" % limit)
        elif _goes_stale(c.type, c.args):
            started, limit, reason = c.created_at, STALE_UNCOLLECTED_S, _stale_reason(c.type)
        else:
            continue
        if not started:
            continue
        # tz-naive rows exist in databases written by the previous build
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
        if (now - started).total_seconds() > limit:
            # conditional: a result (or the bridge's poll) that lands in between wins
            if _move(db, c, (c.status,), status="expired", fail_reason=reason, completed_at=now):
                n += 1
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

# MESH SELF-HEAL LIMITS (2026-09-28). /v1/provision mints a fresh tailnet key for a claimed
# bridge that reports no mesh address. It used to do so on EVERY 15 s poll, with no memory:
#   - a venue that blocks the mesh cost ~5,760 Tailscale API calls and ~5,760 audit rows a day
#     per bridge (kept 365 days), pushing every admin action out of the panel's audit view;
#   - the bridge ran `tailscale up --reset` every tick, and a single empty sample (tailscaled
#     restarting, or a join slower than one tick after claim) re-keyed a WORKING node - new
#     identity, new address, live mesh sessions dropped;
#   - with NB_LAN_ONLY=1 the address is always blank, so every claimed bridge was re-keyed
#     forever onto the mesh that LAN-only mode exists to avoid.
# Now: the address must have been missing for MESH_HEAL_AFTER_S (four reports), no bridge gets
# a second key within _mesh_rekey_gap_s() of the last one (claim, admin re-key or automatic),
# LAN-only mode never re-keys, and only the first automatic key of an outage is audited.
MESH_HEAL_AFTER_S = 60
MESH_REKEY_MIN_GAP_S = 600


def _mesh_rekey_gap_s() -> int:
    # Never replace a key the bridge could still be joining with: it is valid for ts_key_ttl_s.
    return max(MESH_REKEY_MIN_GAP_S, settings.ts_key_ttl_s)


def _note_mesh_address(dev, ip, now) -> None:
    """Record the mesh address a bridge reports, and when it started having none."""
    if LAN_ONLY:
        # Keep the dead mesh IP from creeping back in, and do not start an outage clock for a
        # mesh this deployment deliberately does not use.
        dev.tailscale_ip, dev.mesh_lost_at = None, None
        return
    dev.tailscale_ip = ip or None
    if dev.tailscale_ip:
        dev.mesh_lost_at, dev.mesh_autokeys = None, 0      # on the mesh: any outage is over
    elif dev.mesh_lost_at is None:
        dev.mesh_lost_at = now


def _key_expiry(minted) -> str:
    """When a key the fleet minted stops working (ISO, UTC). Tailscale reports it; when it does not,
    assume the lifetime the fleet asked for (TS_KEY_TTL_S)."""
    exp = minted.get("expires") if isinstance(minted, dict) else None
    return str(exp) if exp else (utcnow() + dt.timedelta(seconds=settings.ts_key_ttl_s)).isoformat()


def _parse_iso(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _mesh_heal_due(dev, now) -> bool:
    """Should /v1/provision mint this bridge a new mesh key right now? See MESH_HEAL_AFTER_S."""
    if LAN_ONLY or not dev.claimed_at or dev.tailscale_ip:
        return False
    lost = auth.as_utc(dev.mesh_lost_at)
    if lost is None or (now - lost).total_seconds() < MESH_HEAL_AFTER_S:
        return False
    last = auth.as_utc(dev.mesh_key_at)
    return last is None or (now - last).total_seconds() >= _mesh_rekey_gap_s()

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

def _ensure_fleet_number_index(conn) -> bool:
    """Make the database refuse a second bridge with the same fleet number in one org
    (2026-09-28; the model declares the same index for new databases).

    If the fleet ALREADY has duplicates, creating the index would fail and take the control
    plane down at start-up. Renumbering a bridge automatically is not an option either: a
    number changes only when an admin changes it. So the index waits, the duplicates are named
    in the log, and it is created on the first start after an admin renumbers them."""
    from sqlalchemy import text as _text
    dups = conn.execute(_text(
        "SELECT org_id, number, COUNT(*) FROM devices WHERE number IS NOT NULL "
        "GROUP BY org_id, number HAVING COUNT(*) > 1")).fetchall()
    if dups:
        print("[migrate] fleet numbers used by more than one bridge: %s. Renumber one of each "
              "(PATCH /admin/devices/{id}); uniqueness is enforced from the next start after that."
              % ", ".join("NB-%03d in org %s (%d bridges)" % (n, org, k) for org, n, k in dups),
              flush=True)
        return False
    conn.execute(_text("CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_org_number "
                       "ON devices (org_id, number)"))
    return True


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
            _ensure_fleet_number_index(conn)
            # Mesh self-heal bookkeeping (2026-09-28): see models.Device.
            dcols = cols("devices")
            if "mesh_lost_at" not in dcols:
                conn.execute(_text("ALTER TABLE devices ADD COLUMN mesh_lost_at DATETIME"))
            if "mesh_key_at" not in dcols:
                conn.execute(_text("ALTER TABLE devices ADD COLUMN mesh_key_at DATETIME"))
            if "mesh_autokeys" not in dcols:
                conn.execute(_text("ALTER TABLE devices ADD COLUMN mesh_autokeys INTEGER DEFAULT 0"))
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
        # Alert storm control (2026-09-28): an episode closes only once its alert has stayed
        # clear for a while. Nullable, so every existing episode reads as "firing" / resolved.
        if "alert_events" in insp.get_table_names():
            if "clear_since" not in cols("alert_events"):
                conn.execute(_text("ALTER TABLE alert_events ADD COLUMN clear_since DATETIME"))
            # A bridge's open episodes, read every second by the panel's stream (models.AlertEvent).
            conn.execute(_text("CREATE INDEX IF NOT EXISTS ix_alert_events_device_resolved "
                               "ON alert_events (device_id, resolved_at)"))
        # Rollouts that say who they left out and why each bridge failed (2026-09-28).
        if "rollouts" in insp.get_table_names() and "excluded" not in cols("rollouts"):
            conn.execute(_text("ALTER TABLE rollouts ADD COLUMN excluded JSON"))
        if "rollout_targets" in insp.get_table_names() and "reason" not in cols("rollout_targets"):
            conn.execute(_text("ALTER TABLE rollout_targets ADD COLUMN reason VARCHAR"))
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


def _purge_orphan_sessions():
    """Once per start: sign-ins left behind by users revoked before 2026-09-28.

    revoke_user() used to delete the user and keep their sessions, and on a database created
    before that date SQLite gives the next new user the revoked user's id - so the leftover
    token signed its old owner in as the new person. Drop every session whose user is gone, and
    every session older than the account it now points at (the id was already reused)."""
    from .db import SessionLocal
    from .models import Session as _S
    db = SessionLocal()
    try:
        users = {u.id: u for u in db.scalars(select(User)).all()}
        stale = [s for s in db.scalars(select(_S)).all()
                 if s.user_id not in users or auth.session_outlived_user(db, s, users[s.user_id])]
        for s in stale:
            db.delete(s)
        if stale:
            db.commit()
            print("[auth] removed %d sign-in(s) left behind by revoked users" % len(stale), flush=True)
    finally:
        db.close()


_purge_orphan_sessions()


@app.on_event("startup")
async def _start_background():
    """Background loops: hourly retention sweep, the alert evaluator that
    pushes new/cleared alerts out by email/webhook (walkthrough J4), and the 30 s
    command/rollout housekeeping (expiry, trial verdicts) - see _housekeeping_loop."""
    import asyncio
    from .db import SessionLocal
    from . import retention, alerting
    asyncio.create_task(retention.sweep_loop(SessionLocal))
    asyncio.create_task(alerting.evaluate_loop(SessionLocal, settings.alert_eval_interval_s))
    asyncio.create_task(_housekeeping_loop())


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
    _note_mesh_address(dev, body.tailscale_ip, utcnow())
    dev.hostname = body.hostname
    dev.token_hash = token_hash  # re-enroll rotates the token
    db.commit()
    # A brand-new device_id = a new SD card contacting the fleet for the first time.
    # That is not a "fault" the level-triggered alert loop would ever catch (a healthy
    # card firing nothing), so record it here, once, as its own edge event.
    if is_new:
        _record_new_device(db, dev)
    return EnrollOut(device_id=dev.id, device_token=token)


def _record_new_device(db, dev):
    """Record a one-time 'new SD card enrolled' event; the alert loop emails it (within one
    ALERT_EVAL_INTERVAL_S, as one digest when a factory run enrols many at once).

    It used to be emailed right here, between the INSERT and the COMMIT: SQLite held its write
    lock for the whole send (up to 8 s webhook + 10 s SMTP), so every other bridge's heartbeat and
    command pull in that window failed with "database is locked" after 5 s (audit, 2026-09-28).
    And it was stored OPEN, so the loop "resolved" it on its next pass and emailed a RESOLVED for
    a card that had simply joined. It is a record, not a problem: born resolved. Best-effort: a
    failure here must never break enrollment (the card still gets its token)."""
    import logging
    from .models import AlertEvent
    try:
        now = utcnow()
        db.add(AlertEvent(device_id=dev.id, kind="new_device", opened_at=now, resolved_at=now,
                          detail="new SD card enrolled: %s (v%s)" % (dev.hostname or dev.id, dev.version or "?")))
        db.commit()
    except Exception:
        logging.getLogger("main").exception("new-device record failed for %s", getattr(dev, "id", "?"))
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
    # The DEVICE is authoritative about its own mesh identity, including its ABSENCE.
    # This used to only write a truthy value, so a bridge that lost its tailnet node
    # (ephemeral nodes are garbage-collected after an outage) kept advertising its old
    # 100.x address forever: the panel showed it "on the mesh", the presenter app was
    # handed a dead IP, and nothing could tell the difference between a healthy bridge
    # and one that had silently fallen off. Clearing it makes the state honest — and is
    # what lets /v1/provision notice the device needs a fresh key and self-heal.
    _note_mesh_address(dev, body.get("tailscale_ip"), now)
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
    now = utcnow()
    out = []
    # At-most-once delivery: mark as "sent" the instant we hand them out, so a
    # command that disrupts the device before it can POST a result is NOT
    # re-pulled every tick (that once looped reset-clock -> gadget teardown).
    for c in rows:
        cid, ctype, args, created = c.id, c.type, dict(c.args or {}), c.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        # Checked HERE as well as by the sweep: a bridge coming back after a weekend polls within
        # seconds, before any timer runs, and must not be handed Friday's reboot (2026-09-28).
        if _goes_stale(ctype, args) and created and (now - created).total_seconds() > STALE_UNCOLLECTED_S:
            _move(db, c, ("pending",), status="expired", fail_reason=_stale_reason(ctype), completed_at=now)
            continue
        # The clock the timeout runs against (sent_at). Without it, "how long has this been out?"
        # could only be answered from created_at, which includes however long the device was
        # offline before it polled - and would expire commands that were never delivered late.
        # Conditional: a cancel that won the race keeps its word, and the bridge never sees it.
        if _move(db, c, ("pending",), status="sent", sent_at=now):
            out.append(CommandOut(id=cid, type=ctype, args=args))
    db.commit()
    return out


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
    #
    # Only done / failed / rejected are results. Anything else is recorded as "failed" with the
    # raw word kept in the output (2026-09-28): it used to be stored verbatim, so "pending" put
    # the command back in the pull queue to run a second time, and an unknown word left it
    # neither finished nor running - its rollout target "updating" for ever.
    status, output = body.status, body.output
    if status not in DEVICE_RESULT_STATES:
        status = "failed"
        output = ("[the device reported status %r, which is not a result the fleet knows; "
                  "recorded as failed]\n%s" % (str(body.status)[:40], body.output or ""))

    # Conditional, so a verdict the sweeper or a cancel reached between our read and our write
    # stands, and this report becomes the late report below. set-pin / unlock carry the PIN in
    # args; _move scrubs it with the result (the device has executed it, so it has no further use
    # here - the Command row used to keep every PIN ever issued, in plaintext, for ever).
    if c.status not in TERMINAL_STATES and _move(db, c, ("pending", "sent"), status=status,
                                                 output=output, completed_at=utcnow()):
        db.commit()
        return {"ok": True}

    # Already final (pending and sent are the only other states, and _move covers both).
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


@app.get("/v1/provision")
def pull_provision(dev: Device = Depends(auth.require_device), db: Session = Depends(get_db)):
    """One-time provisioning payload (secret-at-claim, docs/PROVISIONING-V2.md).

    Returns whatever the admin attached at claim time (e.g. a tailscale auth key)
    and clears it in the same transaction, so the secret is handed out exactly
    once. Subsequent pulls get {"provision": null} — the agent treats that as
    "nothing to do", making it safe to poll every tick.
    """
    payload = dev.provision
    now = utcnow()
    if payload is not None:
        dev.provision = None
        if isinstance(payload, dict):
            payload = dict(payload)
            expires = _parse_iso(payload.pop("_key_expires", None))      # never sent to the bridge
            if (payload.get("tailscale_auth_key") and expires is not None
                    and expires <= now + dt.timedelta(seconds=30)):
                # DEAD ON ARRIVAL (2026-09-28). A key staged while the bridge was off - claimed before
                # it was powered on at the venue, or re-keyed while offline - has expired by the time
                # it is collected. Handing it over costs a failed join AND a full re-key gap (10 min)
                # before self-heal tries again. Mint a live one now instead.
                from . import mesh
                payload.pop("tailscale_auth_key", None)
                try:
                    fresh = mesh.mint_ephemeral_key(
                        "netbridge bridge %s" % (dev.pairing_code or dev.id),
                        tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
                    if fresh.get("key"):
                        payload["tailscale_auth_key"] = fresh["key"]
                    print("[mesh] staged key for %s had expired; issued a fresh one" % dev.id, flush=True)
                except Exception as e:          # includes MeshNotConfigured: nothing live to hand out
                    print("[mesh] staged key for %s had expired; no fresh one: %s" % (dev.id, str(e)[:120]), flush=True)
                if not payload.get("tailscale_auth_key"):
                    dev.mesh_key_at = now       # counts against the gap, like a failed self-heal
                    db.commit()
                    return {"provision": None}
        if isinstance(payload, dict) and (payload.get("tailscale_auth_key")
                                          or payload.get("tailscale_authkey")):
            # The bridge starts joining with this key now. Self-heal must give it time to finish:
            # re-keying on the next poll is what used to reset a node still joining after claim.
            dev.mesh_key_at = now
        db.commit()
        return {"provision": payload}

    # SELF-HEAL: a claimed bridge that is talking to us but has NO mesh identity has lost
    # its tailnet node — bridge nodes are ephemeral, so any outage long enough for the
    # control plane to garbage-collect the node leaves the device with no key and no way
    # to rejoin. It used to take a manual re-key (or SD-card surgery) every single time,
    # which is absurd for a device that is plainly online and authenticated right here.
    # Mint one for it automatically, within the limits described at MESH_HEAL_AFTER_S.
    if not _mesh_heal_due(dev, now):
        return {"provision": None}
    from . import mesh
    try:
        minted = mesh.mint_ephemeral_key(
            "netbridge bridge %s" % (dev.pairing_code or dev.id),
            tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
    except mesh.MeshNotConfigured:
        return {"provision": None}              # fleet has no tailnet: nothing to hand out
    except Exception as e:
        # Tailnet unreachable. The attempt counts against the gap, so a Tailscale outage costs
        # one API call per bridge per gap instead of one per bridge every 15 s.
        dev.mesh_key_at = now
        db.commit()
        print("[mesh] could not re-issue a key for %s: %s" % (dev.id, str(e)[:160]), flush=True)
        return {"provision": None}
    dev.mesh_key_at = now
    if not minted.get("key"):
        db.commit()
        return {"provision": None}
    prov = {"tailscale_auth_key": minted["key"]}
    code = (dev.pairing_code or "").replace("BRIDGE-", "").strip()
    if code:
        prov["tailscale_hostname"] = "netbridge-%s" % code
    first_of_outage = not dev.mesh_autokeys
    dev.mesh_autokeys = (dev.mesh_autokeys or 0) + 1
    db.commit()
    if first_of_outage:
        # Audited as the system, not a person — nobody clicked anything. Once per outage: the
        # retries that follow are the same event, and 144 rows a day would bury real actions.
        _audit(db, auth.Actor("system", dev.org_id, "admin"),
               "mesh-key:auto-reissue", dev.name or dev.pairing_code or dev.id)
    else:
        print("[mesh] re-issued a key for %s again (%d since it lost its mesh address)"
              % (dev.id, dev.mesh_autokeys), flush=True)
    return {"provision": prov}


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


def _open_episodes(db, ids) -> dict:
    """{device_id: {kind: its open, still-firing AlertEvent}} in one query. The panel reads two
    things from it, both exactly as the alert loop sees them (2026-09-28):
      - which alerts are held open by hysteresis: at 73 °C an open "Running hot" stays open until
        the bridge is under 72 °C, so the panel must not call it fine at 73 while the email has
        not said RESOLVED;
      - the alerts only the loop can compute (restart_storm, from telemetry history): it was
        emailed CRITICAL while the panel said "Active" and "Nothing needs you right now"."""
    from .models import AlertEvent
    out = {}
    if ids:
        for e in db.scalars(select(AlertEvent).where(AlertEvent.device_id.in_(list(ids)),
                                                     AlertEvent.resolved_at.is_(None),
                                                     AlertEvent.clear_since.is_(None))
                            .order_by(AlertEvent.id)).all():
            out.setdefault(e.device_id, {}).setdefault(e.kind, e)
    return out


def _busy_reason(dev: Device) -> str | None:
    """Why this bridge must not be interrupted right now, from its last heartbeat - or None.
    The same two conditions bridge-update.sh refuses on (a presenter live, the meeting laptop
    attached); with the laptop attached a media restart has rebooted under-powered bridges."""
    t = dev.latest if isinstance(dev.latest, dict) else {}
    streams = t.get("streams") if isinstance(t.get("streams"), dict) else {}
    if streams.get("video"):
        return "a presenter is live"
    if t.get("udc") == "configured":
        return "the meeting laptop is attached"
    return None


def _safe_alerts(dev: Device, eps=None) -> list:
    """Everything wrong with a bridge now: its snapshot alerts (with the loop's hysteresis), plus a
    restart storm the alert loop has open for it. `eps` = the _open_episodes() map when the caller
    fetched it for many bridges at once; otherwise this bridge's is looked up here."""
    if eps is None:
        from sqlalchemy.orm import object_session
        db = object_session(dev)
        eps = _open_episodes(db, [dev.id]) if db is not None else {}
    mine = eps.get(dev.id, {})
    try:
        alerts = device_alerts(dev, sticky=frozenset(mine))
    except Exception as e:                  # malformed telemetry from one bridge
        print("[alerts] could not evaluate %s: %r" % (dev.id, e))
        alerts = [unreadable_alert(dev)]
    # Offline shows only offline, as device_alerts does.
    if any(a["kind"] == "offline" for a in alerts):
        return alerts
    have = {a["kind"] for a in alerts}
    for kind, e in mine.items():
        if kind in HISTORY_KINDS and kind not in have:
            alerts.append(history_alert(dev, e))
            have.add(kind)
    return alerts


def _device_view(dev: Device, eps=None) -> dict:
    online = is_online(dev)
    alerts = _safe_alerts(dev, eps)
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
    eps = _open_episodes(db, [d.id for d in devs])
    return [_device_view(d, eps) for d in devs]


@app.get("/admin/devices/{device_id}")
def device_detail(device_id: str, actor=Depends(auth.require_admin),
                  db: Session = Depends(get_db)):
    dev = _scoped_device(db, device_id, actor)
    _sweep_expired(db)            # its command list must not show a dead "sent" as in progress
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
        from sqlalchemy.exc import IntegrityError
        try:
            db.commit()
        except IntegrityError:
            # Another renumber or claim took this number after the check above; the unique
            # index refused ours. Same answer as the check, not a 500.
            db.rollback()
            raise HTTPException(409, "%s was just taken by another bridge" % fleet_label(body.number))
        _audit(db, actor, "device:update", "%s: %s" % (dev.name or dev.id, "; ".join(changes)))
    return _device_view(dev)


@app.post("/admin/devices/{device_id}/claim")
def claim_device(device_id: str, body: ClaimIn, actor=Depends(auth.require_admin),
                 db: Session = Depends(get_db)):
    dev = _scoped_device(db, device_id, actor)
    # CLAIM HAPPENS ONCE (2026-09-28). A second claim used to be accepted, and it minted a new
    # mesh key: the bridge then ran `tailscale up --reset`, took a new node and address, and
    # dropped any live mesh session - which is what a script "renaming" a bridge through claim
    # did in the middle of a meeting. Renaming and re-keying have their own endpoints.
    if dev.claimed_at is not None:
        raise HTTPException(409, _already_claimed(dev))
    # The same rule as PATCH: an empty or page-long name was accepted here and nowhere else.
    name = (body.name or "").strip()
    if not 1 <= len(name) <= 64:
        raise HTTPException(400, "name must be 1-64 characters")
    prov = dict(body.provision) if body.provision is not None else {}
    # "Claiming binds it to your org, names it, AND ISSUES ITS MESH-NETWORK KEY. That's the
    # whole enrollment ceremony." (walkthrough J4 step 2). Until now claim only passed
    # through whatever an admin hand-made, so every bridge needed a key minted by hand -
    # the ceremony was three steps, not one. Mint it here when the fleet has mesh
    # configured and the caller did not supply one. Minted BEFORE any database write, so the
    # Tailscale round trip never holds SQLite's write lock while bridges are reporting.
    mint_failed = None
    if not prov.get("tailscale_auth_key"):
        from . import mesh
        try:
            minted = mesh.mint_ephemeral_key(
                "netbridge bridge %s" % (dev.pairing_code or device_id),
                tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
            if minted.get("key"):
                prov["tailscale_auth_key"] = minted["key"]
                prov["_key_expires"] = _key_expiry(minted)     # fleet-side only, see pull_provision
        except mesh.MeshNotConfigured:
            pass          # no TS credential on this fleet: claim still works, just no mesh
        except Exception as e:
            # Never fail a claim because the tailnet is unreachable - the device is claimed
            # either way and can be given a key later.
            mint_failed = str(e)[:120]
    # Name the tailnet node too. Without this every bridge joins as "raspberrypi"
    # and Tailscale de-duplicates with -1/-2 suffixes, so a fleet of bridges is
    # unidentifiable on the mesh. Use the pairing code - the same identifier on the
    # label, in the SSID and in this panel.
    if prov.get("tailscale_auth_key") and not prov.get("tailscale_hostname"):
        code = (dev.pairing_code or "").replace("BRIDGE-", "").strip()
        if code:
            prov["tailscale_hostname"] = "netbridge-%s" % code
    from sqlalchemy import update as _update
    from sqlalchemy.exc import IntegrityError
    for _attempt in range(5):
        now = utcnow()
        # Take the claim atomically: of two admins claiming the same bridge at the same moment,
        # exactly one matches `claimed_at IS NULL`; the other is told it is already claimed.
        won = db.execute(_update(Device).where(Device.id == dev.id, Device.claimed_at.is_(None))
                         .values(claimed_at=now)).rowcount
        if not won:
            db.rollback()
            dev = db.get(Device, device_id)
            if dev is None:
                raise HTTPException(404, "device not found")     # forgotten meanwhile
            raise HTTPException(409, _already_claimed(dev))
        dev.claimed_at = now
        dev.name = name
        if not dev.number:
            dev.number = _next_number(db, dev.org_id)
        if prov:
            dev.provision = prov
        try:
            db.commit()
            break
        except IntegrityError:
            # Another claim in this org took the same next number between our read and our
            # write; the unique index refused the second. Read the numbers again and retry.
            db.rollback()
    else:
        raise HTTPException(503, "could not assign a fleet number; try the claim again")
    if mint_failed:
        _audit(db, actor, "claim:mesh-mint-failed", mint_failed)
    _audit(db, actor, "claim", "%s -> %s" % (dev.pairing_code or device_id, name))
    return _device_view(dev)


def _already_claimed(dev) -> str:
    who = " ".join(x for x in (fleet_label(dev.number), dev.name) if x) or dev.pairing_code or dev.id
    return ("This bridge is already claimed (%s). Rename it (PATCH /admin/devices/{id}) or re-issue "
            "its mesh key (POST /admin/devices/{id}/mesh-key) instead: claiming it again would reset "
            "its mesh connection." % who)


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

    This DELETES the device's history - telemetry and its hourly uptime rollups, alerts,
    commands, diagnostics bundles, its place in rollouts - because a row that outlives its
    device is worse than no row: it attributes the last owner's incidents to the next one.
    (Rollout places and rollups were missed until 2026-09-28: a rollout target left pointing at
    a deleted command stayed "updating" for ever, so that rollout could never widen or finish,
    and a re-enrolled card showed the previous owner's 90-day uptime.) Rollout places go first:
    they reference the commands.
    """
    dev = db.get(Device, device_id)
    if not dev or dev.org_id != actor.org:
        raise HTTPException(404, "no such device")
    label = dev.name or dev.pairing_code or device_id
    from .models import Telemetry, TelemetryRollup, AlertEvent, Command, DiagBundle
    removed = 0
    for model in (RolloutTarget, TelemetryRollup, Telemetry, AlertEvent, Command, DiagBundle):
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
def reissue_mesh_key(device_id: str, body: dict | None = None, actor=Depends(auth.require_admin),
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

    An optional body {"tailscale_auth_key": "..."} stages a key made by hand instead of minting
    one. That used to be done by claiming the bridge again, which is refused since 2026-09-28
    (see claim_device); a fleet with no Tailscale API credential still needs a way to do it.
    """
    dev = _scoped_device(db, device_id, actor)
    supplied = str((body or {}).get("tailscale_auth_key") or "").strip()
    if supplied:
        # Handed to `tailscale up` as one argument on the bridge: no spaces, a sane length.
        if len(supplied) > 256 or any(ch.isspace() for ch in supplied) or not supplied.isprintable():
            raise HTTPException(400, "tailscale_auth_key must be a single key of at most 256 characters")
        key = supplied
    else:
        from . import mesh
        try:
            minted = mesh.mint_ephemeral_key(
                "netbridge bridge %s" % (dev.pairing_code or device_id),
                tags=[t.strip() for t in settings.ts_bridge_tag.split(",") if t.strip()])
        except mesh.MeshNotConfigured:
            raise HTTPException(409, "this fleet has no tailnet credential configured; supply a key "
                                     "as {\"tailscale_auth_key\": \"...\"}")
        except Exception as e:
            raise HTTPException(502, "could not mint a mesh key: %s" % str(e)[:160])
        if not minted.get("key"):
            raise HTTPException(502, "tailnet returned no key")
        key = minted["key"]
    prov = dict(dev.provision or {})
    prov["tailscale_auth_key"] = key
    if supplied:
        prov.pop("_key_expires", None)           # a hand-made key's lifetime is unknown here
    else:
        prov["_key_expires"] = _key_expiry(minted)
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
    # NEWEST FIRST, by number (2026-09-28). A plain string sort listed the oldest first - and would
    # put 2.10 before 2.2 - and both version pickers preselect the first entry, so "Start rollout"
    # with the defaults sent 2.1 to 2.2 bridges. Two builds of one version: the later build first.
    # Anything that is not a version goes last.
    out.sort(key=lambda o: (_version_tuple(o["version"]) or (-1, -1, -1), str(o.get("built") or ""),
                            str(o["version"])), reverse=True)
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
    # Conditional (2026-09-28): the bridge's poll can mark the row "sent" between our read and our
    # write. The cancel used to overwrite that and answer cancelled:true while the bridge ran the
    # command - exactly the dishonesty this endpoint exists to prevent. If the poll won, the
    # re-read status is "sent" and the answer below is the truthful 409.
    if c.status == "pending" and _move(
            db, c, ("pending",), status="cancelled", completed_at=utcnow(),
            fail_reason="cancelled by %s before the device collected it" % (
                getattr(actor, "email", None) or "an operator")):
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


def _args_key(args) -> str:
    return json.dumps(args or {}, sort_keys=True, default=str)


def _describe(ctype: str, args) -> str:
    """"deploy-script bridge-web.py", "update 2.2.1-abc1234" - never a PIN."""
    if ctype in PIN_BEARING_COMMANDS:
        return ctype
    a = args if isinstance(args, dict) else {}
    bits = [str(a[k]) for k in ("name", "version", "mode", "key", "value", "path") if a.get(k) not in (None, "")]
    return " ".join([ctype] + bits)


def _find_in_flight(db: Session, device_id: str, ctype: str, args):
    """-> (identical, other): an identical command (same type AND args) already pending or sent
    on this bridge, else the newest different one of the same type. Never both."""
    rows = db.scalars(select(Command).where(Command.device_id == device_id, Command.type == ctype,
                                            Command.status.in_(("pending", "sent")))
                      .order_by(Command.id.desc())).all()
    want = _args_key(args)
    for c in rows:
        if _args_key(c.args) == want:
            return c, None
    return None, (rows[0] if rows else None)


def _busy_refusal(other, asked_args) -> HTTPException:
    what = _describe(other.type, other.args)
    if other.status == "pending":
        how = ("is still queued on this bridge (it has not collected it yet). Cancel it first if "
               "you want this one instead.")
    else:
        how = ("is still running on this bridge; try again when it finishes (within %d s)."
               % (other.timeout_s or DEFAULT_TIMEOUT_S))
    return HTTPException(409, {"error": "busy", "in_flight": other.id, "status": other.status,
                               "detail": "%s (#%d) %s Not queued: %s." % (
                                   what, other.id, how, _describe(other.type, asked_args))})


_VERSION_NUM = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _version_tuple(v):
    m = _VERSION_NUM.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _running_version(dev) -> str | None:
    """The OS a bridge actually runs. A committed A/B update is the better witness: the version a
    bridge reports comes from a file on its shared /data partition that an OS update does not
    rewrite, so after an update it still names the image the card was flashed with."""
    t = dev.latest if isinstance(dev.latest, dict) else {}
    ota = t.get("ota") if isinstance(t.get("ota"), dict) else {}
    if ota.get("state") == "committed" and ota.get("version"):
        return str(ota["version"])
    return dev.version


def _is_downgrade(dev, target) -> bool:
    have, want = _version_tuple(_running_version(dev)), _version_tuple(target)
    return bool(have and want and want < have)


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
    # NO SILENT DOWNGRADE (2026-09-28). Nothing compared the version asked for with the one the
    # bridge runs, and the version pickers listed the OLDEST first. A 2.1 image installed on a 2.2
    # bridge commits if its health check passes and drops the bridge back to PIN protocol 1: the
    # presenter app refuses it and the fleet can no longer update it remotely - a reflash on site.
    want = (body.args or {}).get("version") if body.type == "update" else None
    if want and not body.allow_downgrade and _is_downgrade(dev, want):
        raise HTTPException(409, {
            "error": "downgrade",
            "detail": ("%s runs %s; %s is OLDER. Installing it is a downgrade (a 2.1 image takes a 2.2 "
                       "bridge off the PIN protocol the presenter app needs). Re-issue with "
                       "allow_downgrade=true if that is really what you want."
                       % (bridge_title(dev), _running_version(dev), want))})
    # A delivered command whose result was lost must be expired BEFORE the in-flight guard
    # below looks, or the new request is "deduplicated" onto that dead row and never queued.
    _sweep_expired(db)
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
    #  2. An in-flight guard for commands that must not double-execute: if an identical one
    #     (same type, same args) is already pending or sent for this device, hand back that one
    #     instead of queuing another. This needs no client change, which is what makes it
    #     actually protective. A DIFFERENT one of the same type is refused, never swapped for
    #     the one in flight (see NO_DOUBLE_EXECUTE).
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
        same, other = _find_in_flight(db, device_id, body.type, body.args)
        if same is not None:
            return {"id": same.id, "status": same.status,
                    "timeout_s": same.timeout_s, "deduplicated": "already_in_flight"}
        if other is not None:
            raise _busy_refusal(other, body.args)

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
    # THE SAME RETRY GUARDS AS ONE BRIDGE (2026-09-28). This loop used to add a row per device with
    # no idempotency key and no in-flight check - the "retried reboot becomes two reboots" class
    # issue_command was fixed for, on the path with the biggest blast radius - and it included
    # unclaimed bench cards and bridges in the middle of a meeting.
    _sweep_expired(db)
    ids, skipped = [], []
    want = (body.args or {}).get("version") if body.type == "update" else None
    interrupts = _goes_stale(body.type, body.args)      # a lock is not among them (INTERRUPTS_MEETING)
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        name = bridge_title(dev)
        if dev.claimed_at is None:
            skipped.append({"device": name, "reason": "not claimed: nobody controls an unclaimed bridge"})
            continue
        if body.type in OLD_SOFTWARE_REFUSALS and _pin_protocol(dev) < 2:
            skipped.append({"device": name, "reason": OLD_SOFTWARE_REFUSALS[body.type]})
            continue
        busy = _busy_reason(dev) if is_online(dev) else None
        if busy and interrupts:
            skipped.append({"device": name, "reason": "%s — a broadcast does not interrupt a meeting; "
                                                      "send it to this bridge on its own if you must" % busy})
            continue
        if want and not body.allow_downgrade and _is_downgrade(dev, want):
            skipped.append({"device": name, "reason": "runs %s, newer than %s (a downgrade)"
                                                      % (_running_version(dev), want)})
            continue
        if body.idempotency_key:
            prior = db.scalars(select(Command).where(Command.device_id == dev.id,
                                                     Command.idempotency_key == body.idempotency_key)
                               .order_by(Command.id.desc()).limit(1)).first()
            if prior is not None:
                ids.append({"device": name, "command_id": prior.id, "deduplicated": "idempotency_key"})
                continue
        if body.type in NO_DOUBLE_EXECUTE:
            same, other = _find_in_flight(db, dev.id, body.type, body.args)
            if same is not None:
                ids.append({"device": name, "command_id": same.id, "deduplicated": "already_in_flight"})
                continue
            if other is not None:
                skipped.append({"device": name, "reason": _busy_refusal(other, body.args).detail["detail"]})
                continue
        # timeout_s was omitted here, so broadcast commands fell back to the column default
        # instead of the per-class table the single-device path uses.
        c = Command(device_id=dev.id, type=body.type, args=body.args or {},
                    timeout_s=_timeout_for(body.type), idempotency_key=body.idempotency_key)
        db.add(c)
        db.flush()
        ids.append({"device": name, "command_id": c.id})
    db.commit()
    _audit(db, actor, "broadcast:%s" % body.type, "%d device(s)" % len(ids))
    return {"queued": ids, "skipped": skipped}


@app.get("/admin/alerts")
def all_alerts(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    out = []
    devs = db.scalars(select(Device).where(Device.org_id == actor.org)).all()
    eps = _open_episodes(db, [d.id for d in devs])
    for dev in devs:
        for a in _safe_alerts(dev, eps):
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
    # Their sign-ins go in the SAME transaction (2026-09-28). This used to delete only the user
    # row: the revoked person's tokens kept pointing at their old id, SQLite gave that id to the
    # next account created, and the old token then signed in as that account - an admin, if an
    # admin was added next. Revoking must end every session, not just hide the name.
    from .models import Session as _S
    db.query(_S).filter(_S.user_id == u.id).delete(synchronize_session=False)
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
INFO_KINDS = notifier.INFO_KINDS                 # records, not problems: the same list the email uses


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
    eps = _open_episodes(db, ids)
    for d in devs:
        online = is_online(d)
        alerts = _safe_alerts(d, eps)
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
    """Send a test message through every configured channel, so an admin can
    confirm their webhook/email is wired WITHOUT unplugging a bridge to trigger a
    real one. Reports exactly which channels fired.

    Its own kind and severity, "[NetBridge TEST] Alert channel check from <admin>": it used to be
    a CRITICAL "offline" page for a bridge called test-bridge (audit, 2026-09-28). It is tried even
    on a channel that is backing off after failures - the admin is asking whether it works now."""
    from . import notifier
    if not notifier.any_channel_configured():
        raise HTTPException(400, "no alert channel configured — set ALERT_WEBHOOK_URL or SMTP_*")
    results = notifier.deliver(notifier.build_test_message(getattr(who, "email", None) or str(who)), force=True)
    return {"sent": results, "ok": any(results.values()), "channels": notifier.channel_status()}


@app.get("/admin/alerts/channels")
def alerts_channels(actor=Depends(auth.require_admin)):
    """Where alerts go and whether that works: email / webhook configured, failing since when,
    the last error and the next attempt. The panel said "Alerts are also emailed" when nothing was
    configured, and nothing anywhere said that email was failing (audit, 2026-09-28). No secrets:
    the webhook shows only its host, the recipient is masked. The channels belong to the fleet's
    operator (OPERATOR_ORG): another organisation's admin learns only whether alerts are delivered."""
    st = notifier.channel_status()
    if actor.org != settings.operator_org:
        return {"any": st["any"], "managed": True,
                "email": {"configured": st["email"]["configured"]}, "webhook": {"configured": st["webhook"]["configured"]}}
    return st


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
            eps = _open_episodes(db, [d.id for d in devs])
            dviews = {d.id: _device_view(d, eps) for d in devs}
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

    def gone(kind, keys):
        """Of these ids that left the snapshot, the ones whose rows no longer exist (a forgotten
        bridge, retention) - as opposed to rows that only scrolled out of the newest-80/60 window."""
        model = Command if kind == "command" else AlertEvent
        db = SessionLocal()
        try:
            still = set(db.scalars(select(model.id).where(model.id.in_(keys))).all())
        finally:
            db.close()
        return [k for k in keys if k not in still]

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
                # Whatever left the snapshot leaves `seen` too (the memory stays bounded; a row
                # that comes back is simply sent again). Until 2026-09-28 only devices did, and
                # the page was never told that a forgotten bridge's commands and alerts were
                # gone: when the same card re-enrolled, its drawer showed the previous owner's
                # history - the very mis-attribution forget_device deletes it to prevent.
                missing = [k for k in s if k not in views]
                for k in missing:
                    del s[k]
                if not missing:
                    continue
                if kind == "device":
                    out.extend(("device_removed", {"id": k}) for k in missing)
                else:
                    try:
                        dead = await asyncio.to_thread(gone, kind, missing)
                    except Exception:
                        dead = []
                    out.extend((kind + "_removed", {"id": k}) for k in dead)
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

# How long a STAGED bridge may take to report the verdict of its trial boot (committed or rolled
# back), counted from the last time the fleet saw it busy: offline, rebooting, or in a meeting.
# The trial is 45 s to the reboot plus up to 300 s of health check (bridge-ab), so 20 minutes of
# silence from a bridge that is online and idle means the trial never completed - a power cut
# mid-trial lands back on the old slot and says nothing.
ROLLOUT_TRIAL_WINDOW_S = 20 * 60

# The version shape bridge-agent.py's _version() accepts. Anything else goes out as a source URL.
_AGENT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9a-f]{7,40})?")

# Target states. In flight: the update is on its way or running (dispatched), or the new OS is in
# the spare slot waiting for its trial verdict (staged). Did not update: the trial rolled back
# (rolled_back), or anything else stopped it (failed). Either kind halts widening.
IN_FLIGHT_TARGET = ("dispatched", "staged")
NOT_UPDATED_TARGET = ("failed", "rolled_back")


def _next_stage(pct: int) -> int | None:
    for s in ROLLOUT_STAGES:
        if s > pct:
            return s
    return None


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _ro_targets(db: Session, ro: Rollout) -> list[RolloutTarget]:
    # Deterministic order so waves are reproducible and an operator can predict
    # which devices go first (never random, never "whoever polled last").
    return db.scalars(
        select(RolloutTarget).where(RolloutTarget.rollout_id == ro.id)
        .order_by(RolloutTarget.device_id)
    ).all()


def _update_in_flight(db: Session, device_id: str):
    return db.scalars(select(Command).where(Command.device_id == device_id, Command.type == "update",
                                            Command.status.in_(("pending", "sent")))
                      .order_by(Command.id.desc()).limit(1)).first()


def _rollout_update_args(ro: Rollout) -> dict:
    """What the `update` command carries. For a version this fleet hosts at its own catalog path,
    the VERSION - exactly what the single-bridge "Install an OS version" sends (2026-09-28): the
    bridge then refuses a manifest that names any other version, and fetches from the fleet address
    it already reaches every 15 s (the panel's own address can be a tunnel the bridge cannot route
    to). Anything else is sent as the source, as before: bridge-update.sh lets --version override
    --url, so the two cannot travel together."""
    src, ver = (ro.source or "").rstrip("/"), ro.version or ""
    if (_AGENT_VERSION.fullmatch(ver) and src.endswith("/payloads/ota/" + ver)
            and os.path.isfile(os.path.join(PAYLOAD_DIR, "ota", ver, "manifest.txt"))):
        return {"version": ver}
    return {"source": ro.source}


def _update_failure(c) -> str:
    """One line on why an update command ended badly, for the rollout card."""
    if c.status == "expired":
        return "the update never reported back (%s)" % (c.fail_reason or "no result")
    lines = [ln.strip() for ln in (c.output or "").splitlines() if ln.strip()]
    last = re.sub(r"^\[ota\]\s*(ERROR:\s*)?", "", lines[-1] if lines else "")[:200]
    if c.status == "rejected":
        return "the bridge rejected the update command: %s" % (last or "no detail")
    if c.status == "failed":
        return "the update failed before its trial boot: %s" % (last or "no detail")
    # a word some older build stored verbatim (results are normalised since 2026-09-28)
    return "the update ended as %r, which is not a result the fleet knows: %s" % (c.status, last or "no detail")


def _trial_verdict(dev: Device, ro: Rollout, since, verified: bool = False):
    """-> (target status, reason) from the bridge's own report of its trial boot, or None while
    there is no verdict yet.

    Only a heartbeat received AFTER the update command finished counts: bridge-update.sh writes
    "staged" before it exits and the agent reports the result after its heartbeat, so anything
    the bridge reports after that is about this attempt, never a leftover from an earlier one.
    Both clocks are the fleet's own (a bridge's clock can be off). Not dev.version: that is read
    from a file an OS update does not rewrite. `verified`: the update carried the version, so the
    bridge itself refused any manifest for another one - a commit that names no version is ours."""
    seen, since = _utc(dev.last_seen), _utc(since)
    if not (seen and since and seen > since):
        return None
    t = dev.latest if isinstance(dev.latest, dict) else {}
    ota = t.get("ota") if isinstance(t.get("ota"), dict) else {}
    state, ver = ota.get("state"), str(ota.get("version") or "")
    detail = str(ota.get("detail") or "").strip()[:200]
    if state == "committed":
        if ver == ro.version or (not ver and verified):
            return "succeeded", None
        return "failed", ("its trial committed %s, not %s — the manifest it installed names another version"
                          % (ver or "an unnamed version", ro.version))
    if state == "rolled back":
        return "rolled_back", "rolled back%s: %s" % (
            " (%s)" % ver if ver and ver != ro.version else "", detail or "the new OS did not come up healthy")
    # "failed" only when it is about THIS version: bridge-update.sh writes "failed" with no version
    # when it refuses before reading a manifest (a later install tried while a meeting was on),
    # which says nothing about the image this rollout staged.
    if state == "failed" and ver == ro.version:
        return "failed", "failed: %s" % (detail or "no detail")
    return None


def _sync_rollout(db: Session, ro: Rollout) -> None:
    """Fold what the bridges report back into target state.

      dispatched -> staged       the update command finished: the new OS is in the spare slot and
                                 the bridge trial-boots it 45 s later
      dispatched -> queued       cancelled, or refused because a meeting was on (nothing installed)
      dispatched -> failed       the update failed before its trial, or never answered
      staged -> succeeded        the bridge reports that the trial COMMITTED this version
      staged -> rolled_back      it reports that the trial rolled back
      staged -> failed           it committed another version, or says nothing for
                                 ROLLOUT_TRIAL_WINDOW_S while online and idle
      any -> removed             the device is no longer in the fleet

    Until 2026-09-28 "done" counted as updated. But done only means STAGED - bridge-update.sh
    --fleet exits 0 before the trial boot - so an image whose trial rolled back on every bridge
    still read "3 of 3 updated · 0 rollbacks" and widened to the whole fleet, two reboots per
    bridge for nothing. And a bridge refusing because its laptop was attached counted as a
    rollback: widening halted over an image nobody had tried.

    Derived on read (and every 30 s by _housekeeping_loop) rather than hooked into the device's
    result and telemetry posts, so the hot device-facing paths stay untouched.
    """
    now = utcnow()
    changed = False
    for t in _ro_targets(db, ro):
        if t.status not in ("queued",) + IN_FLIGHT_TARGET:
            continue
        dev = db.get(Device, t.device_id)
        if dev is None:
            # Its device is gone: it can never finish, and it no longer counts (it used to stay
            # "updating" for ever, so the rollout could never widen or finish).
            t.status, t.reason, t.updated_at, changed = "removed", "no longer in the fleet", now, True
            continue
        if t.status == "dispatched":
            c = db.get(Command, t.command_id) if t.command_id else None
            if c is not None and c.status in ("pending", "sent"):
                continue                                   # on its way, or running
            refused = (REFUSED_WHILE_BUSY.search("%s\n%s" % (c.output or "", c.fail_reason or ""))
                       if c is not None and c.status == "failed" else None)
            if c is None:
                t.status, t.reason = "failed", "its update command no longer exists, so how it ended is unknown"
            elif c.status == "done":
                t.status, t.reason = "staged", None
            elif c.status == "cancelled":
                # An admin recalled it before the bridge collected it: it goes out again.
                t.status, t.command_id, t.reason = "queued", None, "the update was cancelled before the bridge collected it"
            elif refused:
                # Nothing was installed: back in the queue for the next catch-up once it is idle.
                t.status, t.command_id = "queued", None
                t.reason = ("refused while %s — nothing was installed; it goes out again once the bridge is idle"
                            % ("the meeting laptop was attached" if "laptop" in refused.group(0).lower()
                               else "a presenter was live"))
            else:
                # Failed before any trial (download, signature, slot write), or never reported back
                # at all (expired) - which must halt widening just the same rather than leave the
                # target "updating" for ever. Also any status word no build writes any more.
                t.status, t.reason = "failed", _update_failure(c)
            t.updated_at, changed = now, True
        if t.status == "staged":
            c = db.get(Command, t.command_id) if t.command_id else None
            v = _trial_verdict(dev, ro, c.completed_at if c is not None and c.completed_at else t.updated_at,
                               verified=c is not None and (c.args or {}).get("version") == ro.version)
            if v:
                t.status, t.reason, t.updated_at, changed = v[0], v[1], now, True
            elif not is_online(dev) or _busy_reason(dev):
                t.updated_at, changed = now, True     # rebooting, or a meeting is on: the window restarts
            elif (now - _utc(t.updated_at)).total_seconds() > ROLLOUT_TRIAL_WINDOW_S:
                t.status, t.updated_at, changed = "failed", now, True
                t.reason = ("no verdict from its trial boot within %d min of being online and idle — "
                            "it may have lost power mid-trial and come back on the previous OS"
                            % (ROLLOUT_TRIAL_WINDOW_S // 60))
    if changed:
        db.commit()


def _wave_room(targets, ro: Rollout) -> int:
    """How many more bridges the current wave may start. ceil() so a 10% wave over a small fleet
    still moves at least one device; removed targets no longer count."""
    live = [t for t in targets if t.status != "removed"]
    if not live:
        return 0
    return max(1, -(-len(live) * ro.stage_pct // 100)) - sum(1 for t in live if t.status != "queued")


def _same_update(inflight_args, ro: Rollout) -> bool:
    """An update already on its way to a bridge installs what this rollout would: the same version,
    however it was asked for - the panel's "Install on…" sends the version, a rollout the version or
    its source URL, and a forced one adds force (merge of the 2026-09-28 fixes)."""
    a = inflight_args if isinstance(inflight_args, dict) else {}
    return a.get("version") == ro.version or bool((a.get("source") or a.get("url")) == ro.source and ro.source)


def _hold_reason(db: Session, dev: Device, ro: Rollout) -> str | None:
    """Why a queued bridge is not sent its update right now, or None when it can be.
      offline                        "queued until online"
      a meeting on                   laptop attached or a presenter live: bridge-update.sh refuses
                                     then anyway, and that refusal used to be counted as a rollback
                                     that halted the rollout (2026-09-28)
      another OS update in flight    two copies of bridge-update.sh would share the staging
                                     directory and the spare slot (the SAME update is adopted)"""
    if not is_online(dev):
        return "offline"
    busy = _busy_reason(dev)
    if busy:
        return busy
    other = _update_in_flight(db, dev.id)
    if other is not None and not _same_update(other.args, ro):
        return "another OS update is in progress (%s, #%d)" % (_describe(other.type, other.args), other.id)
    return None


def _dispatch_rollout(db: Session, ro: Rollout) -> int:
    """Queue `update` commands up to the current wave, to bridges that can take one NOW.

    The others are deliberately left `queued` (not skipped, not failed) and go out on a later
    dispatch - see _hold_reason. A bridge that meanwhile got to this version another way counts
    as updated, and one whose own page already sent this same update has that command adopted
    rather than a second one queued beside it.
    """
    if ro.status != "active":
        return 0
    targets = [t for t in _ro_targets(db, ro) if t.status != "removed"]
    room = _wave_room(targets, ro)
    args = _rollout_update_args(ro)
    now = utcnow()
    sent, changed = 0, False
    for t in targets:
        if room <= 0:
            break
        if t.status != "queued":
            continue
        dev = db.get(Device, t.device_id)
        if dev is None:
            t.status, t.reason, t.updated_at, changed = "removed", "no longer in the fleet", now, True
            continue
        if _running_version(dev) == ro.version:
            t.status, t.reason, t.updated_at, changed = "succeeded", "already on %s" % ro.version, now, True
            room -= 1
            continue
        if _hold_reason(db, dev, ro):
            continue
        c = _update_in_flight(db, dev.id)          # after _hold_reason: None, or this same update
        if c is None:
            # timeout_s: an OS update may take hours over a venue uplink. Without it the command
            # took the column default (120 s), was marked EXPIRED mid-download, the bridge's later
            # "done" was refused, and the rollout sat "updating" forever and could never widen.
            c = Command(device_id=dev.id, type="update", args=args, timeout_s=_timeout_for("update"))
            db.add(c)
            db.flush()
        t.command_id, t.status, t.wave, t.updated_at, t.reason = c.id, "dispatched", ro.stage_pct, now, None
        room -= 1
        sent += 1
        changed = True
    if sent:
        ro.updated_at = now
    if changed:
        db.commit()
    return sent


def _rollout_view(db: Session, ro: Rollout) -> dict:
    targets = _ro_targets(db, ro)
    by = {s: 0 for s in ("queued", "dispatched", "staged", "succeeded", "failed", "rolled_back", "removed")}
    offline, waiting, ready, verifying, not_updated = [], [], [], [], []
    for t in targets:
        by[t.status] = by.get(t.status, 0) + 1
        if t.status not in ("queued", "staged") + NOT_UPDATED_TARGET:
            continue
        dev = db.get(Device, t.device_id)
        name = bridge_title(dev) if dev else t.device_id
        if t.status in NOT_UPDATED_TARGET:
            not_updated.append({"device": name, "status": t.status,
                                "reason": t.reason or t.status.replace("_", " ")})
        elif t.status == "staged":
            verifying.append(name)
        elif dev is None:
            continue
        else:
            why = _hold_reason(db, dev, ro)
            if why == "offline":
                offline.append(name)
            elif why:
                waiting.append({"device": name, "reason": why})
            else:
                ready.append(name)
    total = len(targets) - by["removed"]
    in_flight = by["dispatched"] + by["staged"]
    room = _wave_room(targets, ro)
    parts = ["%d of %d updated" % (by["succeeded"], total), _plural(by["rolled_back"], "rollback")]
    if by["failed"]:
        parts.append("%d failed" % by["failed"])
    if verifying:
        parts.append("%d verifying the new OS" % len(verifying))
    if offline:
        parts.append("%s queued until online" % ", ".join(offline))
    if waiting:
        parts.append("%s queued until idle" % ", ".join(w["device"] for w in waiting))
    return {
        "id": ro.id, "version": ro.version, "source": ro.source,
        "status": ro.status, "stage_pct": ro.stage_pct,
        "next_stage": _next_stage(ro.stage_pct),
        "total": total,
        "updated": by["succeeded"],
        # the trial boot rolled back: the image itself did not come up healthy
        "rollbacks": by["rolled_back"],
        # did not update for any other reason: failed before its trial, never answered, no verdict
        "failed": by["failed"],
        # both kinds, each with why - any of them halts widening (the "0 rollbacks" guard)
        "failed_devices": not_updated,
        "in_flight": in_flight, "verifying": by["staged"], "queued": by["queued"],
        "queued_offline": offline, "queued_busy": waiting, "queued_ready": ready,
        "verifying_devices": verifying,
        # left out when it started, and why (older than 2.2, unclaimed, already newer)
        "excluded": ro.excluded or [],
        # the current wave still has bridges to send: "Catch up" sends whichever of them can go now
        "catch_up": ro.status == "active" and room > 0 and by["queued"] > 0,
        # the final wave with nothing outstanding: the one moment "Finish" completes it
        "finishable": ro.status == "active" and _next_stage(ro.stage_pct) is None
                      and not by["queued"] and not in_flight,
        "created_by": ro.created_by,
        "created_at": ro.created_at.isoformat() if ro.created_at else None,
        # the walkthrough's one-liner, rendered server-side
        "summary": " · ".join(parts),
    }


@app.post("/admin/rollouts")
def create_rollout(body: RolloutCreateIn, actor=Depends(auth.require_admin),
                   db: Session = Depends(get_db)):
    """Open a staged rollout over the caller's org and dispatch the first wave."""
    if body.stage_pct not in ROLLOUT_STAGES:
        raise HTTPException(400, "stage_pct must be one of %s" % ROLLOUT_STAGES)
    if db.scalar(select(Rollout).where(Rollout.org_id == actor.org, Rollout.status == "active")):
        raise HTTPException(409, "an active rollout already exists for this org")
    _sweep_expired(db)
    ro = Rollout(org_id=actor.org, version=body.version, source=body.source,
                 stage_pct=body.stage_pct, created_by=getattr(actor, "email", "admin"))
    db.add(ro)
    db.flush()
    # Devices already on the target version are not targets — re-running a
    # rollout must never re-flash a device that is already there. Nor are unclaimed bridges, or
    # bridges older than 2.2: their software cannot install an OS version remotely (the update
    # is killed after 30 s, the target sits "updating" until it expires, and widening stalls) -
    # those are flashed by hand (review, 2026-09-25). Nor, unless asked for, bridges that run a
    # NEWER version: see issue_command's downgrade rule (2026-09-28). The rollout keeps the list.
    excluded, targets = [], 0
    for dev in db.scalars(select(Device).where(Device.org_id == actor.org)).all():
        running = _running_version(dev) or ""
        if running == body.version:
            continue
        why = None
        if dev.claimed_at is None:
            why = "not claimed"
        elif _pin_protocol(dev) < 2:
            why = ("runs software older than 2.2, which cannot install an OS version remotely — "
                   "flash its card with the image")
        elif not body.allow_downgrade and _is_downgrade(dev, body.version):
            why = "runs %s, newer than %s — include it only as a deliberate downgrade" % (running, body.version)
        if why:
            excluded.append({"name": bridge_title(dev), "reason": why})
            continue
        db.add(RolloutTarget(rollout_id=ro.id, device_id=dev.id))
        targets += 1
    if not targets:
        db.rollback()                     # an empty rollout would sit "active" doing nothing
        raise HTTPException(409, "no bridge can install %s remotely%s" % (
            body.version, (": " + "; ".join("%s (%s)" % (e["name"], e["reason"]) for e in excluded))
            if excluded else " (they are all on it already)"))
    ro.excluded = excluded
    db.commit()
    sent = _dispatch_rollout(db, ro)
    _audit(db, actor, "rollout:create", "%s -> %d device(s), wave %d%%" %
           (body.version, len(_ro_targets(db, ro)), ro.stage_pct))
    out = _rollout_view(db, ro)
    out["dispatched_now"] = sent
    return out


@app.get("/admin/rollouts")
def list_rollouts(actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    _sweep_expired(db)
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
    _sweep_expired(db)
    _sync_rollout(db, ro)
    view = _rollout_view(db, ro)
    names = {d.id: bridge_title(d) for d in db.scalars(select(Device).where(Device.org_id == ro.org_id)).all()}
    view["devices"] = [
        {"device_id": t.device_id, "device": names.get(t.device_id, t.device_id), "status": t.status,
         "wave": t.wave, "command_id": t.command_id, "reason": t.reason}
        for t in _ro_targets(db, ro)
    ]
    return view


@app.post("/admin/rollouts/{rollout_id}/dispatch")
def dispatch_rollout(rollout_id: int, actor=Depends(auth.require_admin),
                     db: Session = Depends(get_db)):
    """Catch up the current wave — picks up devices that are back online or idle again."""
    ro = _scoped_rollout(db, rollout_id, actor)
    _sweep_expired(db)
    _sync_rollout(db, ro)
    sent = _dispatch_rollout(db, ro)
    out = _rollout_view(db, ro)
    out["dispatched_now"] = sent
    return out


@app.post("/admin/rollouts/{rollout_id}/advance")
def advance_rollout(rollout_id: int, force: bool = False,
                    actor=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """Widen to the next wave, or finish at the last one — refused while the wave is unhealthy.

    This is the "0 rollbacks" guard: any device that did not end up on the new version (its trial
    rolled back, or the update failed) halts the fleet here instead of letting a bad image reach
    everyone, and so does a device still trial-booting it. `force=true` is the deliberate operator
    override. At the final wave `force=true` also finishes a rollout whose remaining bridges are
    still queued (offline, or in meetings): until 2026-09-28 finishing then answered 200 and did
    nothing, and the panel offered "Finish" only in exactly that case - so a rollout every bridge
    had completed could not be finished, and blocked the next one.
    """
    ro = _scoped_rollout(db, rollout_id, actor)
    if ro.status != "active":
        raise HTTPException(409, "rollout is %s" % ro.status)
    _sweep_expired(db)
    _sync_rollout(db, ro)
    view = _rollout_view(db, ro)
    if view["failed_devices"] and not force:
        raise HTTPException(409, "%s did not update (%s) — halting; pass force=true to override" % (
            _plural(len(view["failed_devices"]), "bridge"),
            "; ".join("%s: %s" % (f["device"], f["reason"]) for f in view["failed_devices"])))
    if view["in_flight"] and not force:
        raise HTTPException(409, "%s still updating or trial-booting the new OS — wait for the verdict, "
                                 "or pass force=true" % _plural(view["in_flight"], "bridge"))
    nxt = _next_stage(ro.stage_pct)
    if nxt is None:
        # Already at the widest wave: finish - at once when nothing is outstanding, and only on
        # purpose (force) when some bridges are still queued. They keep the OS they run.
        if (view["queued"] or view["in_flight"]) and not force:
            waiting = (view["queued_offline"] + [w["device"] for w in view["queued_busy"]]
                       + view["queued_ready"] + view["verifying_devices"])
            raise HTTPException(409, "%s not updated yet (%s) — finish anyway with force=true; "
                                     "they keep the OS they run" % (
                                         _plural(view["queued"] + view["in_flight"], "bridge"),
                                         ", ".join(waiting) or "still queued"))
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


# Which states each action may leave (2026-09-28). Any transition used to be accepted: "resume" on
# an aborted or completed rollout made it active again - even beside another active rollout,
# which create_rollout forbids - and both then sent `update` to the same bridges for different
# versions, each staging over the other.
_ROLLOUT_ACTIONS = {"pause": (("active",), "paused"),
                    "resume": (("paused",), "active"),
                    "abort": (("active", "paused"), "aborted")}


@app.post("/admin/rollouts/{rollout_id}/{action}")
def control_rollout(rollout_id: int, action: str, actor=Depends(auth.require_admin),
                    db: Session = Depends(get_db)):
    """pause | resume | abort. Abort stops further waves; it never un-does a
    device that already updated (that is what a new rollout is for)."""
    if action not in _ROLLOUT_ACTIONS:
        raise HTTPException(404, "unknown action")
    ro = _scoped_rollout(db, rollout_id, actor)
    frm, to = _ROLLOUT_ACTIONS[action]
    if ro.status not in frm:
        raise HTTPException(409, "rollout is %s — it cannot be %s" % (
            ro.status, {"pause": "paused", "resume": "resumed", "abort": "aborted"}[action]))
    if action == "resume":
        other = db.scalar(select(Rollout).where(Rollout.org_id == ro.org_id, Rollout.status == "active",
                                                Rollout.id != ro.id))
        if other is not None:
            raise HTTPException(409, "rollout #%d (%s) is active — finish or abort it before resuming "
                                     "this one" % (other.id, other.version))
    ro.status = to
    ro.updated_at = utcnow()
    db.commit()
    _audit(db, actor, "rollout:%s" % action, ro.version)
    return _rollout_view(db, ro)


# Timers that keep command and rollout state true when nobody is looking (2026-09-28): expire the
# commands that can no longer finish, and fold bridge reports into every rollout that still has a
# bridge updating - including one paused or aborted mid-wave. Both used to happen only when the
# panel or an API reader asked, so a staged bridge's trial window could not tell "waiting for a
# meeting to end" from "silent", and expiry depended on a reader.
HOUSEKEEPING_S = 30


def _housekeeping_once():
    from .db import SessionLocal
    db = SessionLocal()
    try:
        _sweep_expired(db)
        busy = select(RolloutTarget.rollout_id).where(RolloutTarget.status.in_(IN_FLIGHT_TARGET))
        for ro in db.scalars(select(Rollout).where(Rollout.id.in_(busy))).all():
            _sync_rollout(db, ro)
    finally:
        db.close()


async def _housekeeping_loop(interval_s: int = HOUSEKEEPING_S):
    import asyncio
    import logging
    log = logging.getLogger("housekeeping")
    while True:
        try:
            await asyncio.to_thread(_housekeeping_once)
        except Exception:                      # never let housekeeping kill the app
            log.exception("command/rollout housekeeping failed; retrying next interval")
        await asyncio.sleep(interval_s)


# Mount the static panel LAST — after every @app route above — so its catch-all "/"
# never shadows an API route (see _mount_panel's note). This must remain the final
# statement that touches `app`. /app (updates) goes first: it is a narrow prefix, but it
# still has to beat the "/" catch-all.
_mount_app_updates()
_mount_payloads()   # narrow prefix, before the "/" catch-all
_mount_panel()
