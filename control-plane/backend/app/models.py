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
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    tailscale_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # sha256 of device token
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
    role: Mapped[str] = mapped_column(String, default="presenter")   # admin | presenter
    invite_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # cleared on redeem
    token_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Every state-changing action, with who did it (walkthrough J4)."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    who: Mapped[str] = mapped_column(String)          # email or "legacy-key"
    action: Mapped[str] = mapped_column(String)       # e.g. command:restart, claim, user:add
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


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
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|done|failed|rejected
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(back_populates="commands")
