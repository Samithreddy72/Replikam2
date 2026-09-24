"""Turn level-triggered detection into edge-triggered notifications.

device_alerts() answers "what is wrong right now" every time it's called. This
module diffs that against the AlertEvent table so each problem pages you exactly
once when it starts and once when it clears — never on a loop while it persists.

Runs as a background task (evaluate_loop), same pattern as retention.sweep_loop.
"""
import datetime as dt
import logging

from sqlalchemy import select

from .alerts import device_alerts, alert_fix
from .models import Device, AlertEvent, utcnow
from . import notifier

log = logging.getLogger("alerting")


def evaluate(db) -> dict:
    """One pass over all devices. Returns counts for logging/tests.
    Idempotent: calling it twice with no state change sends nothing the 2nd time."""
    stats = {"opened": 0, "resolved": 0, "notified": 0, "resolve_notified": 0}
    configured = notifier.any_channel_configured()

    for dev in db.scalars(select(Device)).all():
        # Pass db so restart_storm (history-based) is evaluated here in the loop.
        # The panel read-path calls device_alerts(dev) WITHOUT db to stay a cheap
        # single-snapshot check; restart storms surface via the notification +
        # /admin/alerts/history instead of a live panel card.
        try:
            current = {a["kind"]: a for a in device_alerts(dev, db)}
        except Exception:
            # One bridge's malformed telemetry must not stop every other bridge's alerts.
            log.exception("alert evaluation skipped %s (unreadable telemetry)", dev.id)
            continue
        open_events = {e.kind: e for e in db.scalars(
            select(AlertEvent).where(AlertEvent.device_id == dev.id,
                                     AlertEvent.resolved_at.is_(None))).all()}
        name = dev.name or dev.pairing_code or dev.id

        # 1) NEW alerts: a kind that's firing now with no open event.
        for kind, a in current.items():
            if kind in open_events:
                continue
            ev = AlertEvent(device_id=dev.id, kind=kind, detail=a.get("detail"))
            db.add(ev)
            db.flush()                       # get ev.id / opened_at
            stats["opened"] += 1
            if configured:
                payload = notifier.build_message(
                    name, dev.id, kind, a.get("detail", ""), "firing", alert_fix(kind))
                res = notifier.deliver(payload)
                if any(res.values()):
                    ev.notified_at = utcnow()
                    stats["notified"] += 1

        # 2) RESOLVED alerts: an open event whose kind is no longer firing.
        for kind, ev in open_events.items():
            if kind in current:
                continue
            ev.resolved_at = utcnow()
            stats["resolved"] += 1
            # Only announce a resolution for something we actually announced firing.
            if configured and ev.notified_at:
                payload = notifier.build_message(
                    name, dev.id, kind, ev.detail or "", "resolved", None)
                res = notifier.deliver(payload)
                if any(res.values()):
                    ev.resolve_notified_at = utcnow()
                    stats["resolve_notified"] += 1

        # 3) RETRY: an open, un-notified event (delivery failed earlier, or the
        # channel was configured after it opened). Re-attempt once per tick.
        if configured:
            for kind, ev in open_events.items():
                if ev.notified_at or kind not in current:
                    continue
                payload = notifier.build_message(
                    name, dev.id, kind, ev.detail or "", "firing", alert_fix(kind))
                if any(notifier.deliver(payload).values()):
                    ev.notified_at = utcnow()
                    stats["notified"] += 1

    db.commit()
    if stats["opened"] or stats["resolved"]:
        log.info("alert eval: %s", stats)
    return stats


async def evaluate_loop(session_factory, interval_s: int = 30):
    """Background task: evaluate on startup, then every interval."""
    import asyncio
    def once():
        db = session_factory()
        try:
            evaluate(db)
        finally:
            db.close()
    while True:
        try:
            # In a worker thread: email delivery is blocking SMTP, and on the event loop a slow
            # mail server stalled every request and the panel's live stream (2026-09-25 audit).
            await asyncio.to_thread(once)
        except Exception:
            log.exception("alert evaluation failed; retrying next interval")
        await asyncio.sleep(interval_s)
