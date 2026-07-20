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


def _severity(kind: str) -> str:
    # offline / thermal / lockout are the ones you actually want to wake up for.
    return "critical" if kind in ("offline", "temp_high", "pin_lockout") else "warning"


def build_message(dev_name: str, dev_id: str, kind: str, detail: str,
                  event: str, fix: dict | None) -> dict:
    """The canonical alert payload. `event` is 'firing' or 'resolved'."""
    return {
        "event": event,                       # firing | resolved
        "kind": kind,                         # offline | throttled | temp_high | ...
        "severity": _severity(kind),
        "device": {"name": dev_name, "id": dev_id},
        "detail": detail or "",
        "fix": fix or None,                   # {command,label} when a one-click fix exists
    }


def _send_webhook(payload: dict) -> bool:
    url = settings.alert_webhook_url
    if not url:
        return False
    data = json.dumps(payload).encode()
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
    subject = "[NetBridge %s] %s — %s" % (payload["severity"].upper(), d["name"], payload["kind"])
    lines = [
        "%s: %s" % (verb, payload["kind"]),
        "Bridge : %s (%s)" % (d["name"], d["id"]),
        "Detail : %s" % payload["detail"],
    ]
    if payload.get("fix"):
        lines.append("Fix    : %s" % payload["fix"].get("label", payload["fix"].get("command", "")))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_email_from
    msg["To"] = settings.alert_email_to
    msg.set_content("\n".join(lines))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
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
