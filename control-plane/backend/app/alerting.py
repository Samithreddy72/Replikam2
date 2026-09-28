"""Turn level-triggered detection into edge-triggered notifications.

device_alerts() answers "what is wrong right now" every time it's called. This
module diffs that against the AlertEvent table so each problem pages you exactly
once when it starts and once when it clears — never on a loop while it persists.

Runs as a background task (evaluate_loop), same pattern as retention.sweep_loop.

WHAT PAGES, AND WHEN (2026-09-28 audit - every rule below fixed an email storm or a false email):
  - An alert is recorded the moment it appears, but emailed only once it has lasted
    ALERT_NOTIFY_AFTER_S, and its episode closes only once it has stayed clear
    ALERT_CLEAR_AFTER_S. A reading flickering across a threshold used to email FIRING/RESOLVED on
    every tick (9 emails in 5 minutes from one meeting).
  - Offline is emailed once the bridge has been silent OFFLINE_AFTER_S + ALERT_OFFLINE_CONFIRM_S
    (seen offline on two passes). A brownout reboot is silent for about a minute, and each one
    risked an "offline" email followed by a "resolved" one.
  - The same alert on the same bridge is emailed at most once per ALERT_REPEAT_S.
  - While a bridge is offline, or its telemetry cannot be read, its other episodes are left
    alone: the fleet cannot see them, which is not the same as fixed. They used to be emailed
    RESOLVED, then FIRING again on return.
  - Right after the fleet starts, a bridge it has not heard from yet is not offline: its last
    heartbeat is from before the fleet's own outage. Every bridge used to be paged CRITICAL.
  - An unclaimed bridge is not watched. Its one notice is "new bridge enrolled", sent by this
    loop (not by /v1/enroll, which held the database lock while mailing) and never "RESOLVED".
  - Messages are sent after every state change of the pass is committed, and more than
    ALERT_DIGEST_OVER of them in one pass go out as one digest. When no watched bridge is
    reporting at all, the message says to check the venue's internet and the fleet's own address
    first: that is one fault, not N broken bridges.
"""
import datetime as dt
import logging

from sqlalchemy import func, select

from .alerts import device_alerts, alert_fix, bridge_title, is_online, unreadable_alert, HISTORY_KINDS
from .config import settings
from .models import Device, AlertEvent, utcnow
from . import notifier

log = logging.getLogger("alerting")

# Records, not problems: born resolved (main._record_new_device), announced once, never RESOLVED.
ONE_SHOT_KINDS = frozenset(("new_device",))
# Alerts whose episode detail is kept current while it is open. The ones that name WHAT is affected
# (which services, which parked files), or the history keeps naming the first unit that failed; and
# the ones the panel reads back from their episode (HISTORY_KINDS: restart_storm), or it keeps
# showing the first "3 reboots" however many followed (2026-09-28).
LIVE_DETAIL_KINDS = frozenset(("service_down", "update_rolled_back")) | HISTORY_KINDS
# After the fleet starts, a bridge it has not heard from gets OFFLINE_AFTER_S plus two agent
# ticks (15 s each) to report in before it can count as offline.
STARTUP_SLACK_S = 30
# How long an un-sent RESOLVED email or new-bridge notice is still worth sending (a channel that
# was down, or backing off, catches up; an ancient one is not dug up).
LATE_NOTICE_S = 3600
# Added to every offline email while no watched bridge (two or more) is reporting at all.
FLEET_SILENT_NOTE = ("No bridge in the fleet is reporting. Unless they are all switched off, check the "
                     "venue's internet and the fleet's own address (DNS, HTTPS certificate) first.")


def _utc(t):
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


def _age(now, t) -> float:
    return (now - _utc(t)).total_seconds() if t is not None else float("inf")


def evaluate(db, now: dt.datetime | None = None, started_at: dt.datetime | None = None) -> dict:
    """One pass over all devices. Returns counts for logging/tests.
    Idempotent: calling it twice with no state change sends nothing the 2nd time.
    `now` is the pass's clock (tests move it); `started_at` is when the fleet process started
    (evaluate_loop passes it; None = no start-up grace)."""
    now = now or utcnow()
    stats = {"opened": 0, "resolved": 0, "notified": 0, "resolve_notified": 0, "held": 0, "digests": 0}
    configured = notifier.any_channel_configured()
    grace_until = started_at + dt.timedelta(seconds=settings.offline_after_s + STARTUP_SLACK_S) \
        if started_at is not None else None
    # (payload, on_delivered, stat) - sent at the END, after every write of the pass is committed.
    # SQLite holds its write lock from the first write to the commit, and with an email (seconds)
    # in between every other request waiting to write failed with "database is locked"
    # (review, 2026-09-25). Short transactions only.
    outbox = []
    watched = silent = 0                          # claimed bridges looked at this pass / offline ones

    late_resolves = {}
    if configured:
        for e in db.scalars(select(AlertEvent).where(
                AlertEvent.resolved_at >= now - dt.timedelta(seconds=LATE_NOTICE_S),
                AlertEvent.notified_at.is_not(None), AlertEvent.resolve_notified_at.is_(None),
                AlertEvent.kind.not_in(tuple(ONE_SHOT_KINDS)))).all():
            late_resolves.setdefault(e.device_id, []).append(e)

    for dev in db.scalars(select(Device)).all():
        open_events, changed = {}, False
        for e in db.scalars(select(AlertEvent).where(AlertEvent.device_id == dev.id,
                                                     AlertEvent.resolved_at.is_(None))
                            .order_by(AlertEvent.opened_at, AlertEvent.id)).all():
            if e.kind in ONE_SHOT_KINDS or e.kind in open_events:
                # A new_device record left open by the code before 2026-09-28 (it was then
                # emailed "RESOLVED" 30 s after the card joined), or a duplicate episode: close
                # it without a word.
                e.resolved_at, e.resolve_notified_at, changed = e.opened_at or now, now, True
                continue
            open_events[e.kind] = e
        if changed:
            db.commit()

        # An unclaimed bridge is not watched: a freshly flashed card has no PIN yet and a card
        # enrolled then switched off is silent forever - neither is anyone's emergency. The panel
        # still shows its alerts, under "claim it first". Anything older code left open on it is
        # closed without an email.
        if dev.claimed_at is None:
            for e in open_events.values():
                e.resolved_at, e.resolve_notified_at = now, now
            if open_events:
                db.commit()
            continue

        online = is_online(dev, now)
        if not online and grace_until is not None and now < grace_until:
            continue                  # the fleet itself just came back: this bridge's state is unknown
        watched += 1
        silent += 0 if online else 1

        sticky = {k for k, e in open_events.items() if e.clear_since is None}
        unreadable = False
        try:
            current = {}
            for a in device_alerts(dev, db, now=now, sticky=sticky):
                if a["kind"] in current:          # one episode per kind: keep every detail
                    prev = current[a["kind"]]
                    current[a["kind"]] = dict(prev, detail=", ".join(x for x in (prev.get("detail"), a.get("detail")) if x))
                else:
                    current[a["kind"]] = a
        except Exception:
            # One bridge's malformed telemetry must not stop every other bridge's alerts. It is
            # recorded (and emailed) as the same "Unreadable status" alert the panel shows - it
            # used to be skipped, so the panel showed an alert that no history or email had. What
            # else is wrong with the bridge is unknown, so, as for offline, nothing else resolves.
            log.exception("alert evaluation of %s failed (unreadable telemetry)", dev.id)
            current, unreadable = {"telemetry_unreadable": unreadable_alert(dev)}, True
        name = bridge_title(dev)                  # "NB-001 · Hall" in every email subject

        # 1) FIRING: a new episode, or an open one that is still (or again) firing.
        for kind, a in current.items():
            ev = open_events.get(kind)
            if ev is None:
                ev = AlertEvent(device_id=dev.id, kind=kind, detail=a.get("detail"), opened_at=now)
                db.add(ev)
                db.commit()
                open_events[kind] = ev
                stats["opened"] += 1
                continue
            dirty = False
            if ev.clear_since is not None:        # back before it had stayed clear: same episode
                ev.clear_since, dirty = None, True
            if kind in LIVE_DETAIL_KINDS and a.get("detail") and a["detail"] != ev.detail:
                ev.detail, dirty = a["detail"], True
            if dirty:
                db.commit()

        # 2) CLEARED: an open episode whose alert is no longer firing closes once it has stayed
        # clear ALERT_CLEAR_AFTER_S. Not while the bridge is offline or unreadable: then nothing
        # else is known about it, and "cannot see it" must not be announced as "fixed".
        if online and not unreadable:
            for kind, ev in open_events.items():
                if kind in current:
                    continue
                if ev.clear_since is None:
                    ev.clear_since = now
                    db.commit()
                if _age(now, ev.clear_since) < settings.alert_clear_after_s:
                    continue
                ev.resolved_at = _utc(ev.clear_since)       # when it actually cleared
                db.commit()
                stats["resolved"] += 1
                # Only announce a resolution for something we actually announced firing.
                if configured and ev.notified_at:
                    outbox.append((notifier.build_message(name, dev.id, kind, ev.detail or "", "resolved", None,
                                                          opened_at=ev.opened_at, resolved_at=ev.resolved_at),
                                   _mark(ev, "resolve_notified_at", now), "resolve_notified"))

        # 3) ANNOUNCE: an open episode that is firing and not yet emailed - it has just lasted
        # long enough, or its delivery failed before, or the channel was set up since.
        if configured:
            for kind, ev in open_events.items():
                if ev.notified_at or ev.resolved_at is not None or kind not in current or ev.clear_since is not None:
                    continue
                hold = settings.alert_offline_confirm_s if kind == "offline" else settings.alert_notify_after_s
                if _age(now, ev.opened_at) < hold:
                    stats["held"] += 1            # not yet: a blip (or a reboot) must not page anyone
                    continue
                last = db.scalar(select(func.max(AlertEvent.notified_at)).where(
                    AlertEvent.device_id == dev.id, AlertEvent.kind == kind, AlertEvent.id != ev.id))
                if _age(now, last) < settings.alert_repeat_s:
                    stats["held"] += 1            # emailed about this very alert moments ago
                    continue
                # How often it came and went in the window before this episode began: the email
                # says so, because a held-back re-fire is news only if you know it keeps coming back.
                began = _utc(ev.opened_at)
                repeats = db.scalar(select(func.count(AlertEvent.id)).where(
                    AlertEvent.device_id == dev.id, AlertEvent.kind == kind, AlertEvent.id != ev.id,
                    AlertEvent.opened_at >= began - dt.timedelta(seconds=settings.alert_repeat_s),
                    AlertEvent.opened_at <= began)) or 0
                outbox.append((notifier.build_message(name, dev.id, kind, current[kind].get("detail") or ev.detail or "",
                                                      "firing", alert_fix(kind, dev), opened_at=ev.opened_at,
                                                      repeats=repeats),
                               _mark(ev, "notified_at", now), "notified"))

        # 4) LATE RESOLVED emails: the episode closed while the channel was failing. (Read before
        # this pass changed anything, so nothing resolved just now is in here twice.)
        for ev in late_resolves.get(dev.id, []):
            if ev.resolve_notified_at is None and ev.resolved_at is not None:
                outbox.append((notifier.build_message(name, dev.id, ev.kind, ev.detail or "", "resolved", None,
                                                      opened_at=ev.opened_at, resolved_at=ev.resolved_at),
                               _mark(ev, "resolve_notified_at", now), "resolve_notified"))

    # 5) NEW BRIDGES: the enrolment records this loop has not announced yet, while the bridge still
    # waits to be claimed. Once someone has claimed it there is no news left, and its fix ("Claim
    # it") would be wrong - every notice that goes out carries a fix that applies.
    if configured:
        for ev in db.scalars(select(AlertEvent).where(
                AlertEvent.kind.in_(tuple(ONE_SHOT_KINDS)), AlertEvent.notified_at.is_(None),
                AlertEvent.opened_at >= now - dt.timedelta(seconds=LATE_NOTICE_S))
                .order_by(AlertEvent.id)).all():
            dev = db.get(Device, ev.device_id)
            if dev is None or dev.claimed_at is not None:
                continue
            outbox.append((notifier.build_message(bridge_title(dev), dev.id, ev.kind, ev.detail or "", "firing",
                                                  alert_fix(ev.kind, dev), opened_at=ev.opened_at, severity="info"),
                           _mark(ev, "notified_at", now), "notified"))

    # Every watched bridge silent at once is one fault on the path to the fleet (the venue's
    # internet, the fleet's DNS name or certificate), not N broken bridges: say so where it is read.
    if watched >= 2 and silent == watched:
        for payload, _, _ in outbox:
            if payload["event"] == "firing" and payload["kind"] == "offline":
                payload["note"] = FLEET_SILENT_NOTE

    db.commit()
    _send(db, outbox, stats)
    if stats["opened"] or stats["resolved"] or stats["notified"] or stats["resolve_notified"]:
        log.info("alert eval: %s", stats)
    return stats


def _mark(ev, field, when):
    """What to record once `ev`'s message is delivered: `field` = `when`, on its row by id. It went
    through the ORM object, and when the bridge was forgotten (its rows deleted) while the email was
    going out the UPDATE raised StaleDataError - rolling back the marks of a digest that HAD been
    sent, so the next pass sent it again (2026-09-28). An UPDATE that matches no row is harmless."""
    eid, table = ev.id, AlertEvent.__table__
    def done(db):
        db.execute(table.update().where(table.c.id == eid).values({field: when}))
    return done


def _send(db, outbox, stats):
    """Deliver the pass's messages - one by one, or as ONE digest when there are more than
    ALERT_DIGEST_OVER (a fleet-wide outage is one email, not one per bridge)."""
    if not outbox:
        return
    if len(outbox) > max(1, settings.alert_digest_over):
        if any(notifier.deliver(notifier.build_digest([p for p, _, _ in outbox])).values()):
            for _, done, stat in outbox:
                done(db)
                stats[stat] += 1
            stats["digests"] += 1
            db.commit()
        return
    for payload, done, stat in outbox:
        if any(notifier.deliver(payload).values()):
            done(db)
            stats[stat] += 1
            db.commit()


async def evaluate_loop(session_factory, interval_s: int = 30):
    """Background task: evaluate on startup, then every interval."""
    import asyncio
    started = utcnow()            # bridges get OFFLINE_AFTER_S from here to report in
    def once():
        db = session_factory()
        try:
            evaluate(db, started_at=started)
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
