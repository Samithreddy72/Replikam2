"""Derive alerts from a device's latest telemetry + last_seen (computed on read)."""
import datetime as dt
import re

from sqlalchemy import select

from .config import settings
from .models import Device, utcnow
from .notifier import alert_title, _severity

# What bridge-update.sh says when it refuses because a meeting is on (exit 8): "the meeting laptop
# is attached", or "a presenter session is live" (also matched in the shorter "a presenter is live").
# Nothing was installed. It is not an "OS update failed" alert, and main.py sends a rollout target
# refused this way back to the queue instead of counting it as a failure (2026-09-28).
REFUSED_WHILE_BUSY = re.compile(r"meeting laptop is attached|presenter (?:session )?is live", re.I)


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
        "The bridge went back to its previous OS by itself and is running normally.",
        "Read the reason above, fix the update, then retry it from the Updates page."]},
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
    fix = _FIXES.get(kind)
    if fix is None:
        return None
    out = dict(fix)
    out["args"] = dict(fix.get("args") or {})
    out["steps"] = [s.replace("{setup_ssid}", setup_ssid(dev)) for s in fix.get("steps", [])]
    return out


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
    rate = (pw.get("rate") if isinstance(pw.get("rate"), dict) else {}).get("pct")
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
    for item in L("services"):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            continue
        name, st = item
        if st in ("failed", "inactive", "dead"):
            out.append({"kind": "service_down", "detail": "%s=%s" % (name, st)})
    temp = str(t.get("temp") or "").replace("'C", "").replace("C", "")
    try:
        if float(temp) >= settings.temp_alert_c:
            out.append({"kind": "temp_high", "detail": t.get("temp")})
    except (TypeError, ValueError):
        pass
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
    # PIN brute-force: the device locked itself after 3 wrong tries. This alert IS
    # the "pages its admin" of the walkthrough — no auto-fix (rotate the PIN offline).
    pin = D("pin")
    if pin.get("lockout"):
        try:
            left = int(pin.get("lockout_remaining") or 0)
        except (TypeError, ValueError):
            left = 0
        out.append({"kind": "pin_lockout",
                    "detail": "3 wrong PIN tries — PINs refused for %d more min" % max(1, left // 60)})
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
    um = t.get("usb_misses_per_s")
    if isinstance(um, (int, float)) and um >= settings.usb_miss_alert_per_s:
        out.append({"kind": "usb_misses",
                    "detail": "missing %.1f USB slots/s — video freezes likely" % um})
    free = t.get("data_free_mb")
    if isinstance(free, (int, float)) and free < settings.disk_low_mb:
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
    # A refusal because a meeting was on (laptop attached, presenter live) is not a failure: nothing
    # was installed and the bridge did the right thing. It used to page the owner "OS update failed"
    # for 24 h (2026-09-28).
    refused = ota.get("state") == "failed" and REFUSED_WHILE_BUSY.search(str(ota.get("detail") or ""))
    if ota.get("state") in ("failed", "rolled back") and ota_recent and not refused:
        out.append({"kind": "os_update_failed",
                    "detail": "OS update %s %s — %s" % (ota.get("version") or "", ota["state"],
                                                          ota.get("detail") or "no detail")})
    if D("streams").get("video") and D("mesh_path").get("via") == "relay":
        out.append({"kind": "mesh_relayed",
                    "detail": "the live session is relayed, not direct — expect extra delay"})
    # Restart storm needs history, so it only runs when a db is supplied (the panel
    # read-path and the alert loop both have one; a bare device_alerts(dev) skips it).
    if db is not None:
        storm = restart_storm(dev, db)
        if storm:
            out.append(storm)
    for a in out:
        a["title"] = alert_title(a["kind"])
        a["severity"] = _severity(a["kind"])      # the same list the email uses
        a["fix"] = alert_fix(a["kind"], dev)
    return out
