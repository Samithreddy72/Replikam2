"""Derive alerts from a device's latest telemetry + last_seen (computed on read)."""
import datetime as dt

from sqlalchemy import select

from .config import settings
from .models import Device, utcnow


def is_online(dev: Device) -> bool:
    if not dev.last_seen:
        return False
    last = dev.last_seen
    # SQLite returns tz-naive datetimes; treat them as UTC so the math works on
    # both SQLite (dev) and Postgres (prod).
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    age = (utcnow() - last).total_seconds()
    return age <= settings.offline_after_s



# Known remediations per alert kind (the walkthrough's "known fix attached").
# fix.command must be in the backend ALLOWED_COMMANDS + agent allow-list.
_FIXES = {
    "clock_suspect": {"command": "reset-clock", "args": {}, "label": "Reset audio clock now"},
    "service_down":  {"command": "restart",     "args": {}, "label": "Restart media services"},
    # Return audio running at the wrong rate (pitch-shifted/robotic). The bridge's own
    # watchdog re-opens it within ~10s, so by the time a human reads this it is usually
    # already fixed - the alert's job is the AUDIT TRAIL (it kept happening silently for
    # a whole evening once). The fix restarts media services for the stubborn case.
    "return_mismatch": {"command": "restart", "args": {}, "label": "Re-sync return audio now"},
}


def alert_fix(kind: str):
    return _FIXES.get(kind)


def restart_storm(dev: Device, db, window_min: int = 15,
                  svc_thresh: int = 5, reboot_thresh: int = 3) -> dict | None:
    """A bridge whose media services keep crashing, or that keeps rebooting
    (walkthrough J4: "restart storms … page you"). Needs telemetry HISTORY, not a
    single snapshot — a storm is only visible across time — so this is separate
    from device_alerts and takes a db.

    Each sample carries restarts={feeder_net,uvcd,return_audio} (cumulative
    NRestarts since boot). Walking the series: a rise = a service restarted; a
    fall = the whole Pi rebooted (counters reset to 0). We fire if either the
    total service restarts in the window, or the number of reboots, crosses its
    threshold."""
    from .models import Telemetry
    since = utcnow() - dt.timedelta(minutes=window_min)
    rows = db.scalars(
        select(Telemetry.metrics).where(Telemetry.device_id == dev.id,
                                        Telemetry.ts >= since)
        .order_by(Telemetry.ts)).all()
    totals = []
    for m in rows:
        r = (m or {}).get("restarts") or {}
        if r:
            totals.append(sum(int(v or 0) for v in r.values()))
    if len(totals) < 2:
        return None

    svc_restarts = reboots = 0
    for prev, cur in zip(totals, totals[1:]):
        if cur >= prev:
            svc_restarts += cur - prev       # services restarted this many times
        else:
            reboots += 1                     # counter reset to a lower value = a reboot

    if svc_restarts >= svc_thresh or reboots >= reboot_thresh:
        bits = []
        if svc_restarts >= svc_thresh:
            bits.append("%d service restarts" % svc_restarts)
        if reboots >= reboot_thresh:
            bits.append("%d reboots" % reboots)
        return {"kind": "restart_storm",
                "detail": "%s in %d min" % (" + ".join(bits), window_min)}
    return None


def device_alerts(dev: Device, db=None) -> list[dict]:
    out = []
    if not is_online(dev):
        out.append({"kind": "offline", "detail": "no heartbeat"})
        return out  # offline has no one-click fix; a human must power-cycle it
    t = dev.latest or {}
    # POWER. Alert on what is HAPPENING, not on a flag that can never clear.
    #
    # The raw throttle word carries STICKY bits: once a board has browned out, bit16 and
    # bit18 stay set until it reboots. Alerting on "anything but 0x0" therefore re-fires
    # forever on any board that ever dipped — and it got worse the moment devices started
    # reporting that value honestly instead of a misleading 0x0.
    #
    # Prefer the decoded verdict: alert while it is ACTUALLY browning out, or when the
    # measured rate is high enough to be audible (2%+; 0.5% was confirmed clean by ear).
    # Fall back to the raw word only for older bridges that do not send `power` yet.
    pw = t.get("power") or {}
    rate = (pw.get("rate") or {}).get("pct")
    if pw:
        if pw.get("live"):
            out.append({"kind": "throttled", "detail": "browning out now (%s)" % pw.get("raw")})
        elif rate is not None and rate >= 2.0:
            out.append({"kind": "throttled",
                        "detail": "browning out %.1f%% of recent seconds" % rate})
    else:
        thr = (t.get("throttled") or "").strip()
        if thr and thr not in ("0x0", "throttled=0x0", ""):
            out.append({"kind": "throttled", "detail": thr})

    # SERVICES. systemd's transitional states are not failures.
    #
    # This alerted on anything that was not exactly "active", so a service in `activating`
    # — i.e. starting normally — raised service_down. bridge-feeder-audio cycles routinely,
    # and on 2026-08-12 that produced 18 alerts in 20 minutes, every one emailed. Real
    # failure is `failed` or `inactive`; `activating`, `deactivating` and `reloading` mean
    # systemd is mid-transition and will settle without anyone being told.
    for name, st in (t.get("services") or []):
        if st in ("failed", "inactive", "dead"):
            out.append({"kind": "service_down", "detail": "%s=%s" % (name, st)})
    temp = (t.get("temp") or "").replace("'C", "").replace("C", "")
    try:
        if float(temp) >= settings.temp_alert_c:
            out.append({"kind": "temp_high", "detail": t.get("temp")})
    except (TypeError, ValueError):
        pass
    if t.get("clock_suspect"):
        out.append({"kind": "clock_suspect", "detail": "return-audio I/O errors; run reset-clock"})
    # Rate mismatch: the robotic/pitch-shift class. Published by the bridge's watchdog the
    # moment device pace and pipeline caps disagree; the file (and so this alert) clears on
    # the next successful re-open. Discovered 2026-08-01: 36 or 69 RTP pkts/s where
    # real-time is 50 = exactly this, and NOTHING else in the system could see it.
    mm = t.get("return_mismatch")
    if mm:
        out.append({"kind": "return_mismatch",
                    "detail": "return audio wrong-rate: device %sHz vs pipeline %sHz "
                              "(pitch-shifted; self-heal <10s)" % (
                                  mm.get("device_rate", "?"), mm.get("pipeline_rate", "?"))})
    # PIN brute-force: the device locked itself after 3 wrong tries. This alert IS
    # the "pages its admin" of the walkthrough — no auto-fix (rotate the PIN offline).
    pin = t.get("pin") or {}
    if pin.get("lockout"):
        left = int(pin.get("lockout_remaining") or 0)
        out.append({"kind": "pin_lockout",
                    "detail": "3 wrong PIN tries — bridge locked for %d more min; rotate the PIN if unexpected" % max(1, left // 60)})
    # Restart storm needs history, so it only runs when a db is supplied (the panel
    # read-path and the alert loop both have one; a bare device_alerts(dev) skips it).
    if db is not None:
        storm = restart_storm(dev, db)
        if storm:
            out.append(storm)
    for a in out:
        fix = alert_fix(a["kind"])
        if fix:
            a["fix"] = fix
    return out
