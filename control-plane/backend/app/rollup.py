"""Roll raw telemetry older than 48h into hourly summaries, then delete the raw
rows (walkthrough J4: "Telemetry rolled up after 48 h — the fleet DB stays flat
forever").

This is what lets retention keep the DB flat WITHOUT losing uptime history: the
uptime/SLA endpoint reads minute-precise raw for the recent 48h and these hourly
rollups for everything older. `up_minutes` is stored so an old window scores
exactly the same SLA % it would have from raw.
"""
import datetime as dt
import logging

from sqlalchemy import select, delete

from .config import settings
from .models import Telemetry, TelemetryRollup, utcnow

log = logging.getLogger("rollup")


def _naive(t):
    return t.replace(tzinfo=None) if t and t.tzinfo else t


def rollup_telemetry(db, raw_keep_hours: int | None = None) -> dict:
    """Aggregate then delete raw ticks older than the raw-keep window. Only whole
    hours strictly older than the cutoff hour are rolled, so the boundary hour is
    never half-summarized. Idempotent: rolled raw is deleted, so a second run
    finds nothing to do."""
    raw_keep_hours = raw_keep_hours or settings.rollup_raw_keep_hours
    now = _naive(utcnow())
    # cutoff = start of the oldest hour we still keep raw. Anything in an hour
    # that ends at or before this is complete and safe to roll.
    cutoff_hour = (now - dt.timedelta(hours=raw_keep_hours)).replace(minute=0, second=0, microsecond=0)

    rows = db.execute(
        select(Telemetry.device_id, Telemetry.ts)
        .where(Telemetry.ts < cutoff_hour)
        .order_by(Telemetry.device_id, Telemetry.ts)).all()
    if not rows:
        return {"hours": 0, "raw_deleted": 0}

    # bucket -> {device, hour: {minutes:set, samples, first, last}}
    buckets: dict[tuple, dict] = {}
    for device_id, ts in rows:
        t = _naive(ts)
        hour = t.replace(minute=0, second=0, microsecond=0)
        key = (device_id, hour)
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"minutes": set(), "samples": 0, "first": t, "last": t}
        b["minutes"].add(t.minute)
        b["samples"] += 1
        if t < b["first"]:
            b["first"] = t
        if t > b["last"]:
            b["last"] = t

    for (device_id, hour), b in buckets.items():
        existing = db.get(TelemetryRollup, (device_id, hour))
        if existing:
            # Merge path — only reachable if an hour is rolled twice, which the
            # 48h-floored cutoff makes not happen (an hour is complete before its
            # single roll). Defensive only. NOTE: without storing the minute-set we
            # can't union, so this max() can UNDERCOUNT up_minutes if two partial
            # rolls covered different minutes (0-29 then 30-59 -> 30, not 60). It
            # never overcounts. Bounded by up_minutes never exceeding 60.
            existing.samples += b["samples"]
            existing.up_minutes = min(60, max(existing.up_minutes, len(b["minutes"])))
            existing.first_ts = min(_naive(existing.first_ts), b["first"])
            existing.last_ts = max(_naive(existing.last_ts), b["last"])
        else:
            db.add(TelemetryRollup(device_id=device_id, hour=hour,
                                   samples=b["samples"], up_minutes=len(b["minutes"]),
                                   first_ts=b["first"], last_ts=b["last"]))

    deleted = db.execute(
        delete(Telemetry).where(Telemetry.ts < cutoff_hour),
        execution_options={"synchronize_session": False}).rowcount or 0
    db.commit()
    result = {"hours": len(buckets), "raw_deleted": deleted}
    log.info("rollup: %s", result)
    return result
