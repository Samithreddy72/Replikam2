"""Alert delivery channels (walkthrough J4: "Offline, thermal, restart storms,
and the audio-clock signature page you by email or webhook").

Two channels, both OPTIONAL and both no-ops when unconfigured, so a dev instance
with no SMTP/webhook set never fails and never blocks. A channel that raises is
caught here and the alert stays un-notified (notified_at stays null), so it is
retried once that channel's back-off has passed rather than lost (CHANNEL BACK-OFF).

Nothing here decides WHEN to send — that edge-triggering lives in alerting.py.
This module only knows how to deliver one already-decided message.

A "raw" webhook receives the payload as built here: event = firing | resolved (one alert,
build_message), test (build_test_message) or digest (build_digest: several of one pass, in
`items`, each a firing/resolved payload) - 2026-09-28.
"""
import datetime as dt
import json
import logging
import smtplib
import time
import urllib.parse
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
# Records, not problems: a new bridge joined; an admin's test message. Their email said WARNING
# while the panel said info (2026-09-28).
INFO_KINDS = frozenset(("new_device", "test"))


def _severity(kind: str) -> str:
    return "critical" if kind in CRITICAL_KINDS else "info" if kind in INFO_KINDS else "warning"


# What each alert is called wherever a person reads it: email subjects, the panel, nb.
TITLES = {
    "offline": "Offline", "throttled": "Power: under-voltage", "service_down": "A service is down",
    "temp_high": "Running hot", "clock_suspect": "Audio clock", "return_mismatch": "Return audio at the wrong rate",
    "pin_lockout": "PIN lockout", "pin_not_set": "No PIN set", "pin_gate_unavailable": "PIN media gate off",
    "usb_misses": "USB camera missing frames", "disk_low": "Low disk space", "update_rolled_back": "Update rolled back",
    "safe_mode": "Safe mode", "os_update_failed": "OS update failed", "mesh_relayed": "Relayed connection",
    "restart_storm": "Restart storm", "telemetry_unreadable": "Unreadable status", "new_device": "New bridge enrolled",
    "test": "Test alert",
}


def alert_title(kind: str) -> str:
    return TITLES.get(kind) or str(kind or "alert").replace("_", " ").capitalize()


def _iso(t):
    if t is None:
        return None
    if t.tzinfo is None:                  # SQLite hands back naive datetimes; they are UTC
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.isoformat()


def _when(iso) -> str:
    """'2026-09-28 10:15 UTC' - what a person reads in an email."""
    try:
        return dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return "?"


def _lasted(a, b) -> str:
    try:
        s = int((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return ""
    return "%d s" % s if s < 120 else "%d min" % (s // 60) if s < 7200 else "%d h %02d min" % (s // 3600, (s % 3600) // 60)


def panel_url(dev_id) -> str | None:
    """The bridge's page in the fleet panel, when the fleet knows its public address."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base or not dev_id or dev_id == "TEST":
        return None
    return "%s/#/bridge/%s" % (base, urllib.parse.quote(str(dev_id), safe=""))


def build_message(dev_name: str, dev_id: str, kind: str, detail: str,
                  event: str, fix: dict | None, *, opened_at=None, resolved_at=None,
                  severity: str | None = None, repeats: int = 0) -> dict:
    """The canonical alert payload. `event` is 'firing' or 'resolved'. `repeats` = how many other
    episodes of the same alert this bridge had in the ALERT_REPEAT_S before this one (it is flapping)."""
    return {
        "event": event,                       # firing | resolved
        "kind": kind,                         # offline | throttled | temp_high | ...
        "title": alert_title(kind),           # "No PIN set" - what a person reads
        "severity": severity or _severity(kind),
        "device": {"name": dev_name, "id": dev_id},
        "detail": detail or "",
        "fix": fix or None,                   # {command|None, label, steps}: every alert has one
        "opened_at": _iso(opened_at),
        "resolved_at": _iso(resolved_at),
        "repeats": int(repeats or 0),
        "url": panel_url(dev_id),
        "note": None,                         # set by the alert loop (alerting.FLEET_SILENT_NOTE)
    }


def build_test_message(sent_by: str) -> dict:
    """The 'Send a test alert' message. Its own event and severity: it used to be a CRITICAL
    'offline' page for a bridge called test-bridge, which on a phone's lock screen read exactly
    like a real outage (audit, 2026-09-28)."""
    return {"event": "test", "kind": "test", "title": alert_title("test"), "severity": "info",
            "device": {"name": "Alert channel check", "id": "TEST"}, "sent_by": sent_by,
            "detail": "this is a NetBridge test alert sent by %s - no action needed" % sent_by,
            "fix": None, "opened_at": _iso(dt.datetime.now(dt.timezone.utc)), "resolved_at": None,
            "repeats": 0, "url": None, "note": None}


def build_digest(payloads: list) -> dict:
    """Many messages from ONE evaluation pass as one. A fleet-wide outage (every bridge silent at
    once), the fleet host coming back, or a factory run enrolling ten cards is one email, not
    one per bridge (audit, 2026-09-28)."""
    firing = [p for p in payloads if p["event"] == "firing"]
    sev = ("critical" if any(p["severity"] == "critical" for p in firing) else
           "warning" if any(p["severity"] == "warning" for p in firing) else "info")
    notes = {p.get("note") for p in payloads}
    return {"event": "digest", "severity": sev, "count": len(payloads),
            "firing": len(firing), "resolved": sum(1 for p in payloads if p["event"] == "resolved"),
            "note": notes.pop() if len(notes) == 1 else None,     # said once, when every item carries it
            "items": payloads}


def _summary_line(payload: dict, note: bool = True) -> str:
    """One human-readable line for chat apps."""
    if payload["event"] == "test":
        return "TEST · alert channel check from %s — no action needed" % payload.get("sent_by", "?")
    firing = payload["event"] == "firing"
    icon = ("🔴" if payload["severity"] == "critical" else "🟠" if payload["severity"] == "warning" else "🔵") \
        if firing else "🟢"
    verb = "FIRING" if firing else "RESOLVED"
    d = payload["device"]
    title = payload.get("title") or alert_title(payload["kind"])
    # A recovery names what WAS wrong: "RESOLVED · Offline · Lab — no heartbeat" read as a contradiction,
    # the same way the email's "Detail :" did (2026-09-28).
    what = payload["detail"] or ""
    s = "%s %s · %s · %s — %s" % (icon, verb, title, d["name"], what if firing or not what else "was: " + what)
    if firing and payload.get("fix", {}) and payload["fix"].get("label"):
        s += "  ·  fix: %s" % payload["fix"]["label"]
    if note and payload.get("note"):
        s += "  ·  %s" % payload["note"]
    return s.rstrip(" —")


CHAT_LIMIT = {"discord": 1900, "slack": 3800}       # characters per message, with room to spare


def _webhook_body(payload: dict) -> dict:
    """Shape the payload for the target. Slack wants {text}, Discord wants
    {content}; a raw consumer (n8n, a custom endpoint) gets the structured data.
    Explicit format beats guessing — a raw JSON blob renders as nothing in Slack
    or Discord, which is the whole reason this exists."""
    fmt = (settings.alert_webhook_format or "raw").lower()
    if payload["event"] == "digest":
        head = ((payload["note"] + "\n") if payload.get("note") else "") + \
            "%d NetBridge alert updates:\n" % payload["count"]
        rows = [_summary_line(p, note=not payload.get("note")) for p in payload["items"]]
        line = head + "\n".join(rows)
        # A chat message has a size limit (Discord refuses more than 2,000 characters with HTTP 400,
        # Slack truncates long text). A fleet-wide outage used to fail delivery EVERY pass, so the
        # digest never arrived at all (review, 2026-09-28): keep whole lines, say how many are left out.
        cap = CHAT_LIMIT.get(fmt)
        if cap and len(line) > cap:
            kept = []
            for i, r in enumerate(rows):
                more = "\n…and %d more — open the fleet panel's Alerts page" % (len(rows) - i)
                if len(head) + len("\n".join(kept + [r])) + len(more) > cap:
                    line = head + "\n".join(kept) + more
                    break
                kept.append(r)
    else:
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


def _email_lines(payload: dict, steps: bool = True, note: bool = True) -> list:
    d = payload["device"]
    title = payload.get("title") or alert_title(payload["kind"])
    if payload["event"] == "firing":
        lines = ["FIRING: %s" % title,
                 "Bridge : %s (%s)" % (d["name"], d["id"]),
                 "Detail : %s" % payload["detail"]]
        if note and payload.get("note"):
            lines.append("Note   : %s" % payload["note"])
        if payload.get("opened_at"):
            lines.append("Since  : %s" % _when(payload["opened_at"]))
        if payload.get("repeats"):
            lines.append("Note   : it also fired %d more time%s in the %d min before this began (it keeps coming back)" % (
                payload["repeats"], "" if payload["repeats"] == 1 else "s", max(1, settings.alert_repeat_s // 60)))
        fix = payload.get("fix") or {}
        if fix:
            lines.append("Fix    : %s%s" % (fix.get("label") or fix.get("command") or "",
                                            "  (one click in the fleet panel)" if fix.get("command") else ""))
            if steps:
                for i, step in enumerate(fix.get("steps") or [], 1):
                    lines.append("         %d. %s" % (i, step))
            else:
                lines.append("         (steps as above)")
    else:
        # A recovery says what WAS wrong and when it cleared. It used to repeat the firing detail
        # under "Detail :" ("RESOLVED: No PIN set / Detail : go-live is BLOCKED until you set
        # one"), which read as a contradiction (audit, 2026-09-28).
        lines = ["RESOLVED: %s" % title,
                 "Bridge : %s (%s)" % (d["name"], d["id"]),
                 "Was    : %s" % (payload["detail"] or "—")]
        if payload.get("opened_at"):
            lines.append("Started: %s" % _when(payload["opened_at"]))
        if payload.get("resolved_at"):
            lasted = _lasted(payload.get("opened_at"), payload["resolved_at"])
            lines.append("Cleared: %s%s" % (_when(payload["resolved_at"]), (" (lasted %s)" % lasted) if lasted else ""))
    if payload.get("url"):
        lines.append("Open   : %s" % payload["url"])
    return lines


def _subject(payload: dict) -> str:
    d = payload["device"]
    title = payload.get("title") or alert_title(payload["kind"])
    if payload["event"] == "test":
        return "[NetBridge TEST] Alert channel check from %s" % payload.get("sent_by", "?")
    if payload["event"] == "firing":
        return "[NetBridge %s] %s — %s" % (payload["severity"].upper(), d["name"], title)
    # RESOLVED leads, with no severity: "[NetBridge CRITICAL] … (resolved)" looked exactly like a
    # second page in an inbox or on a lock screen (audit, 2026-09-28).
    return "[NetBridge RESOLVED] %s — %s" % (d["name"], title)


def _digest_subject(payload: dict) -> str:
    items = payload["items"]
    firing = [p for p in items if p["event"] == "firing"]
    resolved = [p for p in items if p["event"] == "resolved"]
    def names(ps):
        titles = []
        for p in ps:
            t = p.get("title") or alert_title(p["kind"])
            if t not in titles:
                titles.append(t)
        return ", ".join(titles[:3]) + (" …" if len(titles) > 3 else "")
    if payload.get("note") and firing and not resolved and all(p["kind"] == "offline" for p in firing):
        return "[NetBridge %s] No bridge is reporting — %d offline at once" % (payload["severity"].upper(), len(firing))
    if firing and resolved:
        return "[NetBridge %s] %d alerts at once — %d firing, %d cleared" % (
            payload["severity"].upper(), len(items), len(firing), len(resolved))
    if firing:
        return "[NetBridge %s] %d alerts at once — %s" % (payload["severity"].upper(), len(firing), names(firing))
    return "[NetBridge RESOLVED] %d alerts cleared — %s" % (len(resolved), names(resolved))


def _send_email(payload: dict) -> bool:
    if not (settings.smtp_host and settings.alert_email_to and settings.alert_email_from):
        return False
    if payload["event"] == "digest":
        subject = _digest_subject(payload)
        lines = ([payload["note"], ""] if payload.get("note") else []) + \
            ["%d alert updates came in one check, so they arrive as one email." % payload["count"], ""]
        # Steps are printed once per DISTINCT fix, not once per kind (review, 2026-09-28): alert_fix()
        # fills the steps in per bridge (the offline fix names THAT bridge's setup Wi-Fi; an unclaimed
        # bridge is told to claim first), so keying on the kind showed bridge B the steps of bridge A.
        seen_fix = set()
        for p in payload["items"]:
            sig = json.dumps(p.get("fix") or {}, sort_keys=True)
            first = sig not in seen_fix
            seen_fix.add(sig)
            lines += _email_lines(p, steps=first, note=not payload.get("note")) + [""]
    elif payload["event"] == "test":
        subject = _subject(payload)
        lines = ["This is a test alert. Nothing is wrong with any bridge.",
                 "Sent by: %s" % payload.get("sent_by", "?"),
                 "If you are reading this, NetBridge alert email works."]
    else:
        subject = _subject(payload)
        lines = _email_lines(payload)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_email_from
    msg["To"] = settings.alert_email_to
    msg.set_content("\n".join(lines))

    _smtp_send(msg)
    return True


def _smtp_send(msg: EmailMessage):
    if settings.smtp_user and not settings.smtp_password:
        # A login with an empty app password can only fail, and a string of failed logins is
        # what gets a Gmail account flagged (audit, 2026-09-28). Say what is wrong instead.
        raise RuntimeError("SMTP_USER is set but SMTP_PASSWORD is empty - not attempting a login")
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


# CHANNEL BACK-OFF (2026-09-28). Every open, un-notified alert was re-sent on every 30 s tick, each
# attempt blocking up to 8 s (webhook) + 10 s (SMTP). With the Gmail app password missing or
# revoked that was a failed Gmail login per open alert every 30 s - 20 a minute with 10 alerts,
# the pattern that gets an account flagged - and a pass took minutes. Now a channel that fails
# is left alone for 30 s, 2 min, then 10 min between attempts; one success resets it. The
# alerts are not lost meanwhile: they stay un-notified and go out when the channel works again.
# In memory, for this process: the fleet runs one worker, and a restart simply tries at once.
# Capped at 10 minutes (review, 2026-09-28): with a 1 h step, a channel that failed four times in a
# row stayed dark for an hour after it recovered, and a brand-new CRITICAL alert was not even tried.
_BACKOFF_S = (30, 120, 600)
_now = time.time                          # tests move the clock
_health = {name: {"fails": 0, "failing_since": None, "last_error": None, "last_ok": None, "next_try": 0.0}
           for name in ("webhook", "email")}


def _webhook_on() -> bool:
    return bool(settings.alert_webhook_url)


def _email_on() -> bool:
    return bool(settings.smtp_host and settings.alert_email_to and settings.alert_email_from)


def _record(name: str, ok: bool, err=None):
    h = _health[name]
    if ok:
        h.update(fails=0, failing_since=None, last_error=None, last_ok=_now(), next_try=0.0)
        return
    h["fails"] += 1
    h["failing_since"] = h["failing_since"] or _now()
    txt = ("%s: %s" % (type(err).__name__, err)) if isinstance(err, BaseException) else str(err or "failed")
    h["last_error"] = _scrub(txt)[:300]
    h["next_try"] = _now() + _BACKOFF_S[min(h["fails"], len(_BACKOFF_S)) - 1]


def deliver(payload: dict, force: bool = False) -> dict:
    """Fan the payload out to every configured channel. Returns {channel: bool}.
    A channel that raises is logged and reported False — the alert is NOT marked
    notified unless at least one channel succeeded, so it retries next tick.
    A channel that is failing is not tried again until its back-off has passed (reported False);
    `force` (the admin's "Send a test alert") tries it anyway.
    Returns {} when no channel is configured at all (so the caller can tell
    'nothing set up' apart from 'everything failed')."""
    results = {}
    for name, on, fn in (("webhook", _webhook_on, lambda p: _send_webhook(p)),
                         ("email", _email_on, lambda p: _send_email(p))):
        if not on():
            continue
        h = _health[name]
        if not force and h["fails"] and _now() < h["next_try"]:
            results[name] = False             # still backing off after a failure
            continue
        try:
            ok = bool(fn(payload))
        except Exception as e:
            log.exception("alert delivery via %s failed", name)
            _record(name, False, e)
            results[name] = False
            continue
        _record(name, ok, None if ok else "the %s did not accept it" % name)
        results[name] = ok
    return results


def _mask_email(addr: str) -> str:
    """'ow…@example.com'; a comma-separated list is masked address by address."""
    if "," in (addr or ""):
        return ", ".join(_mask_email(a.strip()) for a in addr.split(",") if a.strip())
    local, _, domain = (addr or "").partition("@")
    return (local[:2] + "…@" + domain) if domain else ("…" if addr else "")


def _scrub(txt: str) -> str:
    """An error as the panel may show it: without the webhook URL (a Slack URL is a secret), the
    SMTP password, or a full mail address. An SMTP refusal quotes the recipient, and the channel
    status promises a masked one (2026-09-28)."""
    subs = [(settings.alert_webhook_url, "<webhook URL>"), (settings.smtp_password, "<password>")]
    for field in (settings.alert_email_to, settings.smtp_user, settings.alert_email_from):
        subs += [(a.strip(), _mask_email(a.strip())) for a in (field or "").split(",") if "@" in a]
    for secret, mask in sorted(subs, key=lambda sm: -len(sm[0] or "")):    # longest first
        if secret:
            txt = txt.replace(secret, mask)
    return txt


def _utc_iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None


def channel_status() -> dict:
    """Which alert channels are set up and whether they are working - for the panel, so it can
    say where alerts go (or that they go nowhere) and that email is failing. Never the webhook
    URL (a Slack URL is a secret) nor the full recipient address."""
    out = {}
    hook_host = urllib.parse.urlsplit(settings.alert_webhook_url or "").hostname or ""
    for name, on, target in (("email", _email_on(), _mask_email(settings.alert_email_to)),
                             ("webhook", _webhook_on(), hook_host)):
        h = _health[name]
        warn = None
        if name == "email" and on and settings.smtp_user and not settings.smtp_password:
            warn = "SMTP_USER is set but SMTP_PASSWORD is empty"
        out[name] = {"configured": on, "target": target if on else "",
                     "failing": bool(on and h["fails"]), "failures": h["fails"] if on else 0,
                     "failing_since": _utc_iso(h["failing_since"]) if on else None,
                     "last_error": h["last_error"] if on else None,
                     "last_ok": _utc_iso(h["last_ok"]) if on else None,
                     "next_try_at": _utc_iso(h["next_try"]) if on and h["fails"] else None,
                     "warning": warn}
    out["any"] = any_channel_configured()
    return out


def any_channel_configured() -> bool:
    return bool(settings.alert_webhook_url or
                (settings.smtp_host and settings.alert_email_to and settings.alert_email_from))
