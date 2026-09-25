"""Alert delivery channels (walkthrough J4: "Offline, thermal, restart storms,
and the audio-clock signature page you by email or webhook").

Two channels, both OPTIONAL and both no-ops when unconfigured, so a dev instance
with no SMTP/webhook set never fails and never blocks. A channel that raises is
caught by the caller and the alert stays un-notified (notified_at stays null), so
it is retried on the next tick rather than lost.

Nothing here decides WHEN to send — that edge-triggering lives in alerting.py.
This module only knows how to deliver one already-decided message.
"""
import json
import logging
import smtplib
import urllib.request
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("notifier")


# offline / thermal / lockout / a rebooting bridge are the wake-you-up ones.
# critical = someone must act now. The PIN ones block or expose go-live; safe mode means every
# update is off. The rest (USB misses, low disk, a parked update, a failed OS update that
# already rolled itself back, a relayed session) are warnings. ONE list: the email, the
# /admin/alerts/* API and the panel all read it, so they can never disagree.
CRITICAL_KINDS = frozenset(("offline", "temp_high", "pin_lockout", "restart_storm",
                            "pin_not_set", "pin_gate_unavailable", "safe_mode"))


def _severity(kind: str) -> str:
    return "critical" if kind in CRITICAL_KINDS else "warning"


# What each alert is called wherever a person reads it: email subjects, the panel, nb.
TITLES = {
    "offline": "Offline", "throttled": "Power: under-voltage", "service_down": "A service is down",
    "temp_high": "Running hot", "clock_suspect": "Audio clock", "return_mismatch": "Return audio at the wrong rate",
    "pin_lockout": "PIN lockout", "pin_not_set": "No PIN set", "pin_gate_unavailable": "PIN media gate off",
    "usb_misses": "USB camera missing frames", "disk_low": "Low disk space", "update_rolled_back": "Update rolled back",
    "safe_mode": "Safe mode", "os_update_failed": "OS update failed", "mesh_relayed": "Relayed connection",
    "restart_storm": "Restart storm", "telemetry_unreadable": "Unreadable status", "new_device": "New bridge enrolled",
}


def alert_title(kind: str) -> str:
    return TITLES.get(kind) or str(kind or "alert").replace("_", " ").capitalize()


def build_message(dev_name: str, dev_id: str, kind: str, detail: str,
                  event: str, fix: dict | None) -> dict:
    """The canonical alert payload. `event` is 'firing' or 'resolved'."""
    return {
        "event": event,                       # firing | resolved
        "kind": kind,                         # offline | throttled | temp_high | ...
        "title": alert_title(kind),           # "No PIN set" - what a person reads
        "severity": _severity(kind),
        "device": {"name": dev_name, "id": dev_id},
        "detail": detail or "",
        "fix": fix or None,                   # {command|None, label, steps}: every alert has one
    }


def _summary_line(payload: dict) -> str:
    """One human-readable line for chat apps."""
    firing = payload["event"] == "firing"
    icon = ("🔴" if payload["severity"] == "critical" else "🟠") if firing else "🟢"
    verb = "FIRING" if firing else "RESOLVED"
    d = payload["device"]
    s = "%s %s · %s · %s — %s" % (icon, verb, payload["kind"], d["name"], payload["detail"] or "")
    if firing and payload.get("fix", {}) and payload["fix"].get("label"):
        s += "  ·  fix: %s" % payload["fix"]["label"]
    return s.rstrip(" —")


def _webhook_body(payload: dict) -> dict:
    """Shape the payload for the target. Slack wants {text}, Discord wants
    {content}; a raw consumer (n8n, a custom endpoint) gets the structured data.
    Explicit format beats guessing — a raw JSON blob renders as nothing in Slack
    or Discord, which is the whole reason this exists."""
    fmt = (settings.alert_webhook_format or "raw").lower()
    line = _summary_line(payload)
    if fmt == "slack":
        return {"text": line}
    if fmt == "discord":
        return {"content": line}
    return payload            # raw: full structured object


def _send_webhook(payload: dict) -> bool:
    url = settings.alert_webhook_url
    if not url:
        return False
    data = json.dumps(_webhook_body(payload)).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    # Short timeout: a slow webhook must not stall the alert loop for every device.
    with urllib.request.urlopen(req, timeout=8) as r:
        return 200 <= r.status < 300


def _send_email(payload: dict) -> bool:
    if not (settings.smtp_host and settings.alert_email_to and settings.alert_email_from):
        return False
    d = payload["device"]
    verb = "FIRING" if payload["event"] == "firing" else "RESOLVED"
    title = payload.get("title") or alert_title(payload["kind"])
    subject = "[NetBridge %s] %s — %s%s" % (payload["severity"].upper(), d["name"], title,
                                            " (resolved)" if payload["event"] != "firing" else "")
    lines = [
        "%s: %s" % (verb, title),
        "Bridge : %s (%s)" % (d["name"], d["id"]),
        "Detail : %s" % payload["detail"],
    ]
    fix = payload.get("fix") or {}
    if fix:
        lines.append("Fix    : %s%s" % (fix.get("label") or fix.get("command") or "",
                                        "  (one click in the fleet panel)" if fix.get("command") else ""))
        for i, step in enumerate(fix.get("steps") or [], 1):
            lines.append("         %d. %s" % (i, step))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_email_from
    msg["To"] = settings.alert_email_to
    msg.set_content("\n".join(lines))

    _smtp_send(msg)
    return True


def _smtp_send(msg: EmailMessage):
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def email_configured() -> bool:
    return bool(settings.smtp_host and settings.alert_email_from)


def send_mail(to: str, subject: str, body: str) -> bool:
    """Generic transactional email over the same SMTP the alerts use (M6 sign-in).
    Returns False if SMTP isn't configured; raises are the caller's to handle."""
    if not email_configured():
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_email_from
    msg["To"] = to
    msg.set_content(body)
    _smtp_send(msg)
    return True


def deliver(payload: dict) -> dict:
    """Fan the payload out to every configured channel. Returns {channel: bool}.
    A channel that raises is logged and reported False — the alert is NOT marked
    notified unless at least one channel succeeded, so it retries next tick.
    Returns {} when no channel is configured at all (so the caller can tell
    'nothing set up' apart from 'everything failed')."""
    results = {}
    for name, fn in (("webhook", _send_webhook), ("email", _send_email)):
        try:
            ok = fn(payload)
        except Exception:
            log.exception("alert delivery via %s failed", name)
            ok = False
        else:
            if ok is False:
                continue          # channel not configured — omit from results
        results[name] = ok
    return results


def any_channel_configured() -> bool:
    return bool(settings.alert_webhook_url or
                (settings.smtp_host and settings.alert_email_to and settings.alert_email_from))
