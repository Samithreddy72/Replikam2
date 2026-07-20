"""Deterministic retention sweep — keeps the fleet DB flat (walkthrough J4:
"the fleet DB stays flat forever", "gone from today: unbounded telemetry table").

Replaces the old approach of pruning telemetry on ~2% of writes. That was
best-effort in a way that failed exactly when it mattered: a device that stops
reporting never triggers its own cleanup, so the rows of a decommissioned or
long-offline bridge were never collected at all. And it only ever touched
telemetry — audit_log and commands grew without any bound.

This runs on a timer instead of on the write path, so retention no longer
depends on traffic, and every growing table is covered.
"""
import datetime as dt
import logging

from sqlalchemy import delete

from .config import settings
from .models import Telemetry, AuditLog, Command, utcnow

log = logging.getLogger("retention")


def sweep(db) -> dict:
    """Delete anything past its retention window. Returns per-table row counts.
    Safe to call at any time; each table is independent."""
    now = utcnow()
    deleted = {}

    # Raw telemetry. The uptime/SLA endpoint reads a rolling 7-day window of
    # ticks, so this floor cannot go below that without also teaching uptime to
    # read from rollups (that is the phase-2 "rolled up after 48h" item).
    # synchronize_session=False on every delete below: the default ("evaluate")
    # re-runs the WHERE clause in Python against objects already in the session's
    # identity map, and SQLite hands back naive datetimes while our cutoffs are
    # tz-aware — that comparison raises. A bulk sweep has no need to reconcile
    # in-memory state anyway.
    cutoff = now - dt.timedelta(days=settings.telemetry_retention_days)
    deleted["telemetry"] = db.execute(
        delete(Telemetry).where(Telemetry.ts < cutoff),
        execution_options={"synchronize_session": False}).rowcount or 0

    # Audit log. Kept far longer than telemetry — it is the "who did what"
    # record — but not forever, which is what it was before.
    cutoff = now - dt.timedelta(days=settings.audit_retention_days)
    deleted["audit_log"] = db.execute(
        delete(AuditLog).where(AuditLog.ts < cutoff),
        execution_options={"synchronize_session": False}).rowcount or 0

    # Finished commands. Pending/sent ones are left alone at any age: a command
    # still in flight to an offline bridge must survive until that bridge comes
    # back, however long that takes.
    cutoff = now - dt.timedelta(days=settings.command_retention_days)
    deleted["commands"] = db.execute(
        delete(Command).where(Command.created_at < cutoff,
                              Command.status.in_(("done", "failed", "rejected"))),
        execution_options={"synchronize_session": False}).rowcount or 0

    db.commit()
    if any(deleted.values()):
        log.info("retention sweep: %s", deleted)
    return deleted


async def sweep_loop(session_factory, interval_s: int = 3600):
    """Background task: sweep on startup, then every hour."""
    import asyncio
    while True:
        try:
            db = session_factory()
            try:
                sweep(db)
            finally:
                db.close()
        except Exception:                      # never let retention kill the app
            log.exception("retention sweep failed; will retry next interval")
        await asyncio.sleep(interval_s)
