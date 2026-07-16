"""Derive alerts from a device's latest telemetry + last_seen (computed on read)."""
import datetime as dt

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
}


def alert_fix(kind: str):
    return _FIXES.get(kind)


def device_alerts(dev: Device) -> list[dict]:
    out = []
    if not is_online(dev):
        out.append({"kind": "offline", "detail": "no heartbeat"})
        return out  # offline has no one-click fix; a human must power-cycle it
    t = dev.latest or {}
    thr = (t.get("throttled") or "").strip()
    if thr and thr not in ("0x0", "throttled=0x0", ""):
        out.append({"kind": "throttled", "detail": thr})
    for name, st in (t.get("services") or []):
        if st != "active":
            out.append({"kind": "service_down", "detail": "%s=%s" % (name, st)})
    temp = (t.get("temp") or "").replace("'C", "").replace("C", "")
    try:
        if float(temp) >= settings.temp_alert_c:
            out.append({"kind": "temp_high", "detail": t.get("temp")})
    except (TypeError, ValueError):
        pass
    if t.get("clock_suspect"):
        out.append({"kind": "clock_suspect", "detail": "return-audio I/O errors; run reset-clock"})
    # PIN brute-force: the device locked itself after 3 wrong tries. This alert IS
    # the "pages its admin" of the walkthrough — no auto-fix (rotate the PIN offline).
    pin = t.get("pin") or {}
    if pin.get("lockout"):
        left = int(pin.get("lockout_remaining") or 0)
        out.append({"kind": "pin_lockout",
                    "detail": "3 wrong PIN tries — bridge locked for %d more min; rotate the PIN if unexpected" % max(1, left // 60)})
    for a in out:
        fix = alert_fix(a["kind"])
        if fix:
            a["fix"] = fix
    return out
