"""SQLAlchemy models for the fleet control plane."""
import datetime as dt

from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, Text, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class Device(Base):
    __tablename__ = "devices"

    # device_id = the Pi CPU serial (stable across reflash).
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="default")  # reserved for multi-tenant
    pairing_code: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)       # set when claimed
    # Fleet number, shown as NB-001: assigned at claim (next free in the org), stable for
    # the life of the device, changeable only by an admin (PATCH /admin/devices/{id}).
    number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    tailscale_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # sha256 of device token
    # Setup-AP passphrase, reported by the agent on the authenticated channel. Kept in its
    # OWN column and stripped from the telemetry blob, so it is never part of `latest` (which
    # every device view returns) nor of the retained Telemetry history. This is what makes a
    # label reprintable after a reflash regenerates it (build-ledger E1).
    setup_pass: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # most recent telemetry blob
    # One-time provisioning payload (secret-at-claim flow, docs/PROVISIONING-V2.md):
    # attached by the admin at claim time, handed to the device exactly once via
    # GET /v1/provision (which clears it). Never exposed in admin device views.
    provision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    commands: Mapped[list["Command"]] = relationship(back_populates="device")


class Telemetry(Base):
    __tablename__ = "telemetry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    metrics: Mapped[dict] = mapped_column(JSON)


class User(Base):
    """A person with their own credential (phase 5). role: admin | presenter.
    Invite flow: created with a one-time invite token (hash stored); redeeming
    it mints the personal bearer token (hash stored). No plaintext at rest."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    org_id: Mapped[str] = mapped_column(String, default="default", index=True)  # multi-tenant scope
    role: Mapped[str] = mapped_column(String, default="presenter")   # admin | presenter
    invite_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # cleared on redeem
    token_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Magic-link sign-in (M6): a one-time login code (hashed) + its expiry. Set on
    # POST /auth/magic-link, consumed by POST /auth/magic-redeem to mint the bearer.
    login_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    login_expires: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Every state-changing action, with who did it (walkthrough J4)."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    who: Mapped[str] = mapped_column(String)          # email or "bootstrap-key"
    org_id: Mapped[str] = mapped_column(String, default="default", index=True)  # scope of the action
    action: Mapped[str] = mapped_column(String)       # e.g. command:restart, claim, user:add
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TelemetryRollup(Base):
    """Hourly summary of raw telemetry (walkthrough J4: "Telemetry rolled up
    after 48 h — the fleet DB stays flat forever"). Raw ticks (~15s) are kept
    48h for minute-precise recent uptime; older ticks collapse to one row per
    (device, hour) here and the raw rows are deleted. `up_minutes` is the count
    of DISTINCT minutes in that hour that had at least one tick — the exact same
    quantity the uptime endpoint sums for its SLA %, so a rolled window scores
    identically to a raw one."""
    __tablename__ = "telemetry_rollup"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    hour: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)  # truncated to the hour, UTC
    samples: Mapped[int] = mapped_column(Integer)          # raw ticks that hour
    up_minutes: Mapped[int] = mapped_column(Integer)       # distinct minutes with a tick, 0..60
    first_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class AlertEvent(Base):
    """One row per (device, alert-kind) episode — the memory that turns
    level-triggered detection (device_alerts, recomputed on every read) into
    edge-triggered notifications. Without this, a bridge that stays offline for
    three days would send an alert email on every evaluation tick.

    Lifecycle: created when an alert first appears (opened_at set, notified_at
    set once delivery succeeds); resolved_at set when it clears. A recurrence
    after resolution is a NEW row, so 'offline again' pages again."""
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    kind: Mapped[str] = mapped_column(String, index=True)       # offline | throttled | ...
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolve_notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiagBundle(Base):
    """A diagnostics bundle the device collected and uploaded (walkthrough J4:
    'Bundle ready … Download bundle'). Bundles are small tgz files (~15-50 KB);
    stored inline, newest 3 per device kept."""
    __tablename__ = "diag_bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Command(Base):
    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    type: Mapped[str] = mapped_column(String)        # restart | reset-clock | profile | set-peer
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending | sent | done | failed | rejected | cancelled | expired
    #
    # Before 2026-08-25 the vocabulary stopped at "sent", and nothing ever moved a command out
    # of it: if the agent died, rebooted, or simply never reported, the row stayed "sent"
    # forever. Two were observed stuck for over an hour while later commands completed, with
    # nothing in the UI to say so. `expired` and `cancelled` exist so that every command
    # reaches a terminal state and an operator can tell WHY it did.
    status: Mapped[str] = mapped_column(String, default="pending")
    # Caller-supplied retry key. Two POSTs with the same key for the same device return the
    # SAME command instead of queuing a second one -- a retried `reboot` must not become two
    # reboots. Nullable because it is optional: the in-flight guard in issue_command protects
    # the dangerous types without any client change.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # When the device actually took it. The gap between created_at and sent_at is queue wait;
    # the gap between sent_at and now is what the timeout is measured against.
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Seconds allowed between delivery and a result, chosen per command CLASS - a read takes
    # seconds, a reboot has to survive the reboot itself, an OTA has to survive a download over
    # a venue uplink. One global timeout would either kill legitimate slow work or let a dead
    # command sit for an hour.
    timeout_s: Mapped[int] = mapped_column(Integer, default=120)
    # Why it ended badly. An operator asking "what happened to that?" currently has nothing.
    fail_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    device: Mapped[Device] = relationship(back_populates="commands")


class Rollout(Base):
    """A staged fleet image update (walkthrough J4: "A/B image update, staged
    10% -> 100% · 22 of 25 updated · 0 rollbacks · SF Lab queued until online").

    The per-DEVICE engine already exists: bridge-update.sh verifies the signed
    manifest, writes the STANDBY slot, tryboots it, and the on-device health
    check auto-commits or auto-rolls-back. This model is only the FLEET-side
    orchestration on top — which devices, in what wave, and how far to go.
    """
    __tablename__ = "rollouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String, default="default", index=True)
    version: Mapped[str] = mapped_column(String)     # target image version, e.g. 1.4.2
    source: Mapped[str] = mapped_column(String)      # base URL/dir handed to bridge-update.sh
    stage_pct: Mapped[int] = mapped_column(Integer, default=10)      # current wave: 10/25/50/100
    status: Mapped[str] = mapped_column(String, default="active")    # active|paused|completed|aborted
    # Bridges left out when the rollout started (older than 2.2, unclaimed, or already on a newer
    # version) as [{"name", "reason"}]. Recorded at creation so the card can keep saying who is NOT
    # covered; before 2026-09-28 only the create response named them and the panel never showed it.
    excluded: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    targets: Mapped[list["RolloutTarget"]] = relationship(back_populates="rollout")


class RolloutTarget(Base):
    """One device's place in a rollout.

    A target stays `queued` while its device is offline — that is exactly the
    walkthrough's "SF Lab queued until online": the wave does not skip it and
    does not fail it, it simply waits and dispatches when the device reappears.
    The same while a meeting is on there (laptop attached, presenter live).
    """
    __tablename__ = "rollout_targets"

    rollout_id: Mapped[int] = mapped_column(ForeignKey("rollouts.id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    # queued | dispatched | staged | succeeded | rolled_back | failed | removed
    #   staged       the update command finished: the new OS is in the spare slot and the bridge is
    #                trial-booting it. NOT success yet - the trial's health check decides (2026-09-28:
    #                "done" used to count as updated, so a trial that rolled back was never seen).
    #   rolled_back  the trial did not come up healthy and the bridge went back to its old OS
    #   failed       did not update for any other reason (see `reason`)
    #   removed      the device no longer exists; it no longer counts toward the rollout
    status: Mapped[str] = mapped_column(String, default="queued")
    command_id: Mapped[int | None] = mapped_column(ForeignKey("commands.id"), nullable=True)
    wave: Mapped[int | None] = mapped_column(Integer, nullable=True)   # stage_pct it went out in
    # Why the target is where it is, in words an operator can act on: why it failed, or why a
    # queued one was sent back (the bridge refused because a meeting was on).
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # For a staged target: the last moment the fleet saw it busy (offline, rebooting, a meeting
    # on). The trial window runs from here, so a trial that waits for a meeting is not failed.
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rollout: Mapped[Rollout] = relationship(back_populates="targets")


class Session(Base):
    """One live sign-in. A user may hold SEVERAL at once.

    The bearer token used to live on the user row, so every sign-in overwrote the last one:
    signing into the presenter app silently logged you out of the fleet panel, and a second
    device logged out the first. Being signed in on the panel AND the app at the same time is
    the normal case, not an edge case. Each sign-in now gets its own row, revocable
    independently. User.token_hash is kept so tokens minted before this still work."""
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    token_hash: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
