"""Derive alerts from a device's latest telemetry + last_seen (computed on read)."""
import datetime as dt
import logging
import math
import re

from sqlalchemy import select

from .config import settings
from .models import Device, utcnow
from .notifier import alert_title, _severity

log = logging.getLogger("alerts")


def is_online(dev: Device, now: dt.datetime | None = None) -> bool:
    if not dev.last_seen:
        return False
    last = dev.last_seen
    # SQLite returns tz-naive datetimes; treat them as UTC so the math works on
    # both SQLite (dev) and Postgres (prod).
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    age = ((now or utcnow()) - last).total_seconds()
    return age <= settings.offline_after_s


def pin_protocol(dev) -> int:
    """2 = the bridge's PIN gate from the 2026-09-25 image; 1 = older software (or unknown).
    The same rule as main._pin_protocol, which refuses clear-lockout below 2."""
    t = getattr(dev, "latest", None)
    pin = t.get("pin") if isinstance(t, dict) else None
    try:
        return int(pin.get("protocol") or 1) if isinstance(pin, dict) else 1
    except (TypeError, ValueError, OverflowError):      # JSON lets a bridge send Infinity
        return 1


def _num(v):
    """A telemetry reading as a finite float, or None. Bridges send JSON, and JSON as Python reads it
    also carries "3.5" (a string), true, NaN and Infinity: compared or formatted as numbers they
    raised inside device_alerts, and the alert loop then skipped the whole bridge - no email for
    its PIN lockout or its heat - while the panel said "Unreadable status" (2026-09-28)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None



# EVERY ALERT CARRIES ITS OWN FIX (owner rule, 2026-09-25). Two shapes:
#   one click   {"command": <in ALLOWED_COMMANDS and the agent allow-list>, "args", "label", "steps"}
#               the panel routes it through the same action path as the menu, so set-pin opens its
#               dialog and reboot / unquarantine / revert ask for confirmation exactly as they do there;
#   hands-on    {"command": None, "label", "steps"}: nothing in software can fix it (power, heat, a
#               bridge that is switched off), so the steps say exactly what a person does.
# "steps" is what the panel shows under "How to fix", and what the alert email lists.
# tests/test-fleet-alerts.py fails if any kind this module can raise has no fix, or names a command
# the server or the bridge would refuse.
_FIXES = {
    "offline": {"command": None, "label": "How to bring it back", "steps": [
        "Check it has power: the red light on the Pi is on.",
        "Moved to a new place? After a minute without Wi-Fi it opens its setup Wi-Fi {setup_ssid} - join it "
        "from a phone (password: Setup label in its details) and pick the venue's Wi-Fi.",
        "Still missing? Unplug the meeting laptop, then power-cycle the bridge.",
        "It shows up here again within a minute of coming back."]},
    "throttled": {"command": None, "label": "Fix the power", "steps": [
        "It is browning out (under-voltage): audio stutters and it can reboot in the middle of a meeting.",
        "Check the power cable and its connection to the Pi - reseat it; avoid long or thin cables.",
        "This is electrical: restarting services will not fix it."]},
    "service_down": {"command": "restart", "args": {}, "label": "Restart media services", "steps": [
        "Unplug the meeting laptop first - restarting the camera with it attached can reboot the bridge.",
        "Restart the media services (about 10 s; a live session drops and comes back).",
        "If it fails again, open Logs for the service named above."]},
    "temp_high": {"command": None, "label": "Cool it down", "steps": [
        "Give it air: out of direct sun, not in a closed box or bag, nothing stacked on it.",
        "It slows itself down when hot, which drops video frames.",
        "The alert clears by itself once it has cooled."]},
    "clock_suspect": {"command": "reset-clock", "args": {}, "label": "Reset audio clock now", "steps": [
        "Rebuilds the USB audio device: the meeting laptop's camera, mic and speaker drop and come back - "
        "re-select them in the meeting app.",
        "Best done between meetings."]},
    # Return audio at the wrong rate (pitch-shifted/robotic). The bridge's own watchdog re-opens it
    # within ~10 s, so by the time a human reads this it is usually fixed - the alert is the AUDIT
    # TRAIL (it once went on silently for a whole evening). The button is for the stubborn case.
    "return_mismatch": {"command": "restart", "args": {}, "label": "Re-sync return audio now", "steps": [
        "The bridge usually fixes this itself within 10 s.",
        "If it keeps coming back: unplug the meeting laptop, then restart media services."]},
    "pin_lockout": {"command": "clear-lockout", "args": {}, "label": "Clear the lockout", "steps": [
        "Someone typed 3 wrong PINs, so the bridge refuses every PIN for an hour.",
        "A presenter mistyping? Clear the lockout.",
        "Not expected? Set a new PIN instead - that clears the lockout too."]},
    # The same alert on a bridge from before the 2026-09-25 image: it has no clear-lockout, and
    # the fleet refuses to send one (409). Offering the button - and printing "Fix: Clear the
    # lockout" in the email - promised a fix that could not run (audit, 2026-09-28).
    "pin_lockout@old": {"command": None, "label": "Wait for it to end", "steps": [
        "Someone typed 3 wrong PINs, so the bridge refuses every PIN for an hour.",
        "This bridge runs software from before 2026-09-25, which cannot clear a lockout remotely: "
        "it ends by itself.",
        "Update the bridge from the Updates page so a lockout can be cleared with one click next time."]},
    "pin_not_set": {"command": "set-pin", "args": {}, "label": "Set a PIN…", "steps": [
        "Every bridge needs a PIN: without one, going live is refused (older bridges stay open to anyone).",
        "Set a 4-8 digit PIN and give it to your presenters."]},
    "pin_gate_unavailable": {"command": "reboot", "args": {}, "label": "Reboot to re-arm the gate…", "steps": [
        "The PIN still guards go-live, but video and voice are not blocked at the network level.",
        "The bridge retries every 15 s by itself.",
        "If this stays, reboot it outside a meeting (unplug the meeting laptop first)."]},
    "usb_misses": {"command": None, "label": "Fix the USB link", "steps": [
        "Plug the meeting laptop straight into the bridge - no USB hub - with a short data cable.",
        "Check the Power line in the bridge's details: brownouts cause missed frames too.",
        "Still freezing? Collect diagnostics from the Diagnostics tab."]},
    "disk_low": {"command": None, "label": "Free up space", "steps": [
        "/data holds logs, diagnostics bundles and installed updates.",
        "Revert installed updates you no longer need (Installed updates -> Revert).",
        "If it keeps filling, collect diagnostics and check which part grows."]},
    "update_rolled_back": {"command": "unquarantine", "args": {}, "label": "Put the parked update back…", "steps": [
        "The bridge undid an update that failed its health check and now runs the built-in file.",
        "Put it back only once the cause is fixed - otherwise revert it for good."]},
    "safe_mode": {"command": "revert-script", "args": {}, "label": "Revert an update…", "steps": [
        "3 unhealthy boots in a row, so every installed update is switched off this boot.",
        "Revert the most recent update (the usual cause), then reboot.",
        "Nothing installed recently? Collect diagnostics."]},
    "os_update_failed": {"command": None, "label": "What to do", "steps": [
        "The bridge is still running its previous OS: a failed or rolled-back update never replaces it.",
        "Read the reason above. If a meeting was in progress, install again after it; otherwise fix the update, then retry it from the Updates page."]},
    "mesh_relayed": {"command": None, "label": "Get a direct connection", "steps": [
        "The venue network blocks a direct connection, so the session runs through a relay: expect extra delay.",
        "Try the presenter on another network (a phone hotspot usually works), or ask the venue to allow UDP."]},
    "restart_storm": {"command": "diagnose", "args": {}, "label": "Collect diagnostics", "steps": [
        "Media keeps crashing or the bridge keeps rebooting.",
        "Usual causes: power (see the Power line) or a recent update (see Installed updates - revert it).",
        "The diagnostics bundle shows which service fails."]},
    "telemetry_unreadable": {"command": None, "label": "What to do", "steps": [
        "The bridge sent status the fleet could not read - usually software out of step with the fleet.",
        "Check its Version; update it from the Updates page or reflash it with the current image."]},
    "new_device": {"command": None, "label": "Claim it", "steps": [
        "A new SD card joined the fleet. Open the fleet, find it under New and claim it: that names it, gives "
        "it a fleet number and its mesh key.",
        "Not yours? Leave it unclaimed - an unclaimed bridge cannot be used or controlled."]},
}


def bridge_title(dev) -> str:
    """How a person names a bridge everywhere: "NB-001 · Hall" (an unclaimed one by its code)."""
    num = getattr(dev, "number", None)
    return " · ".join(x for x in (("NB-%03d" % num) if num else None,
                                  getattr(dev, "name", None) or getattr(dev, "pairing_code", None) or getattr(dev, "id", None)) if x)


def setup_ssid(dev) -> str:
    """The setup Wi-Fi a bridge opens when it has no network: the same rule as bridge-wifi-portal.sh
    ("BridgeSetup-" + the pairing code without its "BRIDGE-" prefix)."""
    code = (getattr(dev, "pairing_code", None) or "").strip()
    return "BridgeSetup-" + (code[len("BRIDGE-"):] if code.startswith("BRIDGE-") else code or "…")


def alert_fix(kind: str, dev=None):
    """The fix for one alert kind, as a fresh copy (callers may add to it); steps filled in for `dev`."""
    if kind == "pin_lockout" and dev is not None and pin_protocol(dev) < 2:
        kind = "pin_lockout@old"
    fix = _FIXES.get(kind)
    if fix is None:
        return None
    out = dict(fix)
    out["args"] = dict(fix.get("args") or {})
    out["steps"] = [s.replace("{setup_ssid}", setup_ssid(dev)) for s in fix.get("steps", [])]
    return out


def _counter(v):
    """One NRestarts value as an int, or None when the bridge sent something that is not a number.
    int(v or 0) raised on a single 'n/a', which made the alert loop skip the WHOLE bridge - no PIN
    lockout or heat email for 15 minutes, while the panel still showed them (audit, 2026-09-28)."""
    if isinstance(v, bool):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


_UPTIME_UNITS = {"year": 525600, "week": 10080, "day": 1440, "hour": 60, "minute": 1}


def uptime_minutes(text) -> int | None:
    """`uptime -p` as the bridge reports it ("2 days, 3 hours, 4 minutes") in minutes, or None."""
    parts = re.findall(r"(\d+)\s*(year|week|day|hour|minute)s?\b", str(text or ""))
    return sum(int(n) * _UPTIME_UNITS[u] for n, u in parts) if parts else None


def restart_storm(dev: Device, db, window_min: int = 15,
                  svc_thresh: int = 5, reboot_thresh: int = 3, now: dt.datetime | None = None) -> dict | None:
    """A bridge whose media services keep crashing, or that keeps rebooting
    (walkthrough J4: "restart storms … page you"). Needs telemetry HISTORY, not a
    single snapshot — a storm is only visible across time — so this is separate
    from device_alerts and takes a db.

    Each sample carries restarts={feeder_net,uvcd,return_audio} (cumulative NRestarts since
    boot): a rise between two samples = a service restarted. A REBOOT is told apart by the
    sample's boot_id changing (bridges from 2026-09-28 report it). Older bridges have no
    boot_id, so a reboot there is the counters falling OR the reported uptime going down.
    Counters alone missed exactly the fleet's known failure: a healthy bridge browning out and
    resetting reads 0 -> 0 across every reboot, so a board resetting every minute never fired
    this (audit, 2026-09-28). We fire if either the service restarts in the window, or the
    number of reboots, crosses its threshold."""
    from .models import Telemetry
    since = (now or utcnow()) - dt.timedelta(minutes=window_min)
    rows = db.scalars(
        select(Telemetry.metrics).where(Telemetry.device_id == dev.id,
                                        Telemetry.ts >= since)
        .order_by(Telemetry.ts)).all()
    samples = []                                  # (boot_id, restart total, uptime minutes)
    for m in rows:
        m = m if isinstance(m, dict) else {}
        r = m.get("restarts") if isinstance(m.get("restarts"), dict) else {}
        nums = [n for n in (_counter(v) for v in r.values()) if n is not None]
        total = sum(nums) if nums else None
        boot = m.get("boot_id") if isinstance(m.get("boot_id"), str) and m["boot_id"].strip() else None
        up = uptime_minutes(m.get("uptime"))
        if total is None and boot is None and up is None:
            continue
        samples.append((boot, total, up))
    if len(samples) < 2:
        return None

    svc_restarts = reboots = 0
    for (pb, pt, pu), (cb, ct, cu) in zip(samples, samples[1:]):
        if pb and cb:
            rebooted = pb != cb
        else:
            rebooted = (pt is not None and ct is not None and ct < pt) or \
                       (pu is not None and cu is not None and cu < pu)
        if rebooted:
            reboots += 1
        elif pt is not None and ct is not None:
            svc_restarts += max(0, ct - pt)       # services restarted this many times

    if svc_restarts >= svc_thresh or reboots >= reboot_thresh:
        bits = []
        if svc_restarts >= svc_thresh:
            bits.append("%d service restarts" % svc_restarts)
        if reboots >= reboot_thresh:
            bits.append("%d reboots" % reboots)
        return {"kind": "restart_storm",
                "detail": "%s in %d min" % (" + ".join(bits), window_min)}
    return None


# Alerts that need telemetry HISTORY: the alert loop computes them, and the panel reads them back
# from their open episode instead of re-reading 15 minutes of telemetry every second for every
# bridge. Without that, restart_storm was emailed CRITICAL while the panel showed the bridge
# "Active" and "Nothing needs you right now" (audit, 2026-09-28).
HISTORY_KINDS = frozenset(("restart_storm",))


def history_alert(dev, ev) -> dict:
    """A panel alert for an open HISTORY_KINDS episode (same shape as device_alerts' entries)."""
    return {"kind": ev.kind, "title": alert_title(ev.kind), "severity": _severity(ev.kind),
            "detail": ev.detail or "", "fix": alert_fix(ev.kind, dev)}


def unreadable_alert(dev) -> dict:
    """What stands in for a bridge's alerts when device_alerts cannot read its telemetry. The panel
    and the alert loop use this same alert, so what the panel shows is also what is recorded and
    emailed (2026-09-28)."""
    return {"kind": "telemetry_unreadable", "title": alert_title("telemetry_unreadable"),
            "severity": _severity("telemetry_unreadable"),
            "detail": "this bridge sent telemetry the fleet could not read",
            "fix": alert_fix("telemetry_unreadable", dev)}


def device_alerts(dev: Device, db=None, now: dt.datetime | None = None, sticky=()) -> list[dict]:
    """What is wrong with this bridge right now. `sticky` = kinds whose episode is open and still
    firing: those clear at a lower threshold than they fire at (hysteresis), so a reading hovering
    at the limit stays one alert instead of flickering. The alert loop and the panel pass the same
    set (main._open_episodes), so the panel never says "fine" while the email says "firing"."""
    out = []
    if not is_online(dev, now):
        # No command can reach a bridge that is off: its fix is the hands-on steps.
        out.append({"kind": "offline", "title": alert_title("offline"), "severity": _severity("offline"),
                    "detail": "no heartbeat",
                    "fix": alert_fix("offline", dev)})
        return out
    t = dev.latest if isinstance(dev.latest, dict) else {}
    # Telemetry comes from devices, so every nested field is checked for its shape: one odd value
    # must not hide a bridge's other alerts (or, before 2026-09-25, 500 the whole fleet list).
    D = lambda k: t.get(k) if isinstance(t.get(k), dict) else {}
    L = lambda k: t.get(k) if isinstance(t.get(k), list) else []
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
    pw = D("power")
    rate = _num((pw.get("rate") if isinstance(pw.get("rate"), dict) else {}).get("pct"))
    if pw:
        if pw.get("live"):
            out.append({"kind": "throttled", "detail": "browning out now (%s)" % pw.get("raw")})
        elif rate is not None and rate >= 2.0:
            out.append({"kind": "throttled",
                        "detail": "browning out %.1f%% of recent seconds" % rate})
    else:
        thr = str(t.get("throttled") or "").strip()
        if thr and thr not in ("0x0", "throttled=0x0", ""):
            out.append({"kind": "throttled", "detail": thr})

    # SERVICES. systemd's transitional states are not failures.
    #
    # This alerted on anything that was not exactly "active", so a service in `activating`
    # — i.e. starting normally — raised service_down. bridge-feeder-audio cycles routinely,
    # and on 2026-08-12 that produced 18 alerts in 20 minutes, every one emailed. Real
    # failure is `failed` or `inactive`; `activating`, `deactivating` and `reloading` mean
    # systemd is mid-transition and will settle without anyone being told.
    #
    # ONE service_down naming every failed unit. One row per unit collapsed to the last one in
    # the alert loop (it keys episodes by kind), so with three units down the email and the
    # history named only bridge-return-audio (audit, 2026-09-28) - and the panel showed three
    # identical "Restart media services" buttons for one restart.
    down = []
    for item in L("services"):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            continue
        name, st = item
        if st in ("failed", "inactive", "dead"):
            down.append("%s=%s" % (name, st))
    if down:
        out.append({"kind": "service_down", "detail": ", ".join(down)})
    # HYSTERESIS (2026-09-28): an open temp_high / usb_misses alert (in `sticky`) clears only below
    # its clear level, not the moment the reading dips under the level it fired at. Inside that band
    # the detail says so, or "Running hot" at 73 °C under a 75 °C limit reads as a mistake.
    temp = str(t.get("temp") or "").replace("'C", "").replace("C", "")
    try:
        tc = float(temp)
    except (TypeError, ValueError):
        tc = None
    if tc is not None and math.isfinite(tc):
        clear = min(settings.temp_clear_c, settings.temp_alert_c)
        if tc >= settings.temp_alert_c:
            out.append({"kind": "temp_high",
                        "detail": "SoC at %.1f °C (limit %g °C)" % (tc, settings.temp_alert_c)})
        elif "temp_high" in sticky and tc >= clear:
            out.append({"kind": "temp_high",
                        "detail": "SoC at %.1f °C (limit %g °C; the alert clears below %g °C)"
                                  % (tc, settings.temp_alert_c, clear)})
    if t.get("clock_suspect"):
        out.append({"kind": "clock_suspect", "detail": "return-audio I/O errors"})
    # Rate mismatch: the robotic/pitch-shift class. Published by the bridge's watchdog the
    # moment device pace and pipeline caps disagree; the file (and so this alert) clears on
    # the next successful re-open. Discovered 2026-08-01: 36 or 69 RTP pkts/s where
    # real-time is 50 = exactly this, and NOTHING else in the system could see it.
    mm = D("return_mismatch")
    if mm:
        out.append({"kind": "return_mismatch",
                    "detail": "return audio wrong-rate: device %sHz vs pipeline %sHz "
                              "(pitch-shifted; self-heal <10s)" % (
                                  mm.get("device_rate", "?"), mm.get("pipeline_rate", "?"))})
    # PIN brute-force: the device locked itself after 3 wrong tries. This alert IS the "pages
    # its admin" of the walkthrough. Bridges from the 2026-09-25 image take a one-click
    # clear-lockout; older ones cannot clear it remotely and wait out the hour (alert_fix).
    pin = D("pin")
    if pin.get("lockout"):
        try:
            left = int(pin.get("lockout_remaining") or 0)
        except (TypeError, ValueError, OverflowError):
            left = 0
        out.append({"kind": "pin_lockout",
                    "detail": "3 wrong PIN tries — PINs refused for %d more min%s" % (
                        max(1, left // 60),
                        "" if pin_protocol(dev) >= 2 else
                        " (ends by itself: this bridge's software cannot clear it remotely)")})
    # PIN. Every bridge must be PIN-gated: without a PIN anyone who can reach it over the
    # mesh can go live on it. Bridges from the 2026-09-24 image refuse go-live outright
    # until a PIN is set (pin.required); older ones stay open, which is worse.
    if pin and pin.get("pin_set") is False:
        out.append({"kind": "pin_not_set",
                    "detail": ("go-live is BLOCKED until you set one"
                               if pin.get("required") else
                               "anyone on the mesh can go live on this bridge")})
    if pin.get("gate") == "unavailable":
        out.append({"kind": "pin_gate_unavailable",
                    "detail": "the media gate could not be armed — go-live still needs the PIN, "
                              "but media is not blocked at the network level"})
    # USB camera: missed isochronous slots per second (the bridge's camera service counts them).
    um = _num(t.get("usb_misses_per_s"))
    if um is not None:
        clear = min(settings.usb_miss_clear_per_s, settings.usb_miss_alert_per_s)
        if um >= settings.usb_miss_alert_per_s:
            out.append({"kind": "usb_misses",
                        "detail": "missing %.1f USB slots/s — video freezes likely" % um})
        elif "usb_misses" in sticky and um >= clear:
            out.append({"kind": "usb_misses",
                        "detail": "missing %.1f USB slots/s (the alert clears below %g/s)" % (um, clear)})
    free = _num(t.get("data_free_mb"))
    if free is not None and free < settings.disk_low_mb:
        out.append({"kind": "disk_low", "detail": "only %d MB free on /data" % free})
    parked = [str(x) for x in L("quarantined")]
    if parked:
        out.append({"kind": "update_rolled_back",
                    "detail": "automatic rollback parked %s — the bridge runs the built-in "
                              "file instead" % ", ".join(parked)})
    if D("overrides").get("safe_mode"):
        out.append({"kind": "safe_mode",
                    "detail": "3 boots in a row were unhealthy — every update is OFF this boot"})
    ota = D("ota")
    try:
        ota_recent = (dt.datetime.now(dt.timezone.utc).timestamp() - float(ota.get("ts") or 0)) < 24 * 3600
    except (TypeError, ValueError):
        ota_recent = False
    if ota.get("state") in ("failed", "rolled back") and ota_recent:
        # An update that failed before its manifest was read has no version: no double space.
        out.append({"kind": "os_update_failed",
                    "detail": "%s — %s" % (" ".join(str(x) for x in ("OS update", ota.get("version"), ota["state"]) if x),
                                           ota.get("detail") or "no detail")})
    if D("streams").get("video") and D("mesh_path").get("via") == "relay":
        out.append({"kind": "mesh_relayed",
                    "detail": "the live session is relayed, not direct — expect extra delay"})
    # Restart storm needs history, so it only runs when a db is supplied (the alert loop; the
    # panel reads it back from its open episode - HISTORY_KINDS). Its failure must never hide
    # the snapshot alerts above: one odd history sample used to silence the whole bridge.
    if db is not None:
        try:
            storm = restart_storm(dev, db, now=now)
        except Exception:
            log.exception("restart_storm skipped for %s (unreadable history)", getattr(dev, "id", "?"))
            storm = None
        if storm:
            out.append(storm)
    for a in out:
        a["title"] = alert_title(a["kind"])
        a["severity"] = _severity(a["kind"])      # the same list the email uses
        a["fix"] = alert_fix(a["kind"], dev)
    return out
