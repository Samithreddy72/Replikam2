"""Control-plane configuration, all from environment.

Single-org for now; ORG_ID is reserved in the schema for future multi-tenant.
"""
import os


class Settings:
    # SQLite by default for local dev; point at Postgres in production, e.g.
    #   postgresql+psycopg://bridge:bridge@localhost/bridge
    database_url = os.getenv("DATABASE_URL", "sqlite:///./bridge.db")

    # Bootstrap tokens flashed Pis present at first enrollment, as a comma list of
    # `token` or `token:org`. A bare token enrolls the device into org "default";
    # `token:acme` enrolls it directly into org "acme" (multi-tenant). Rotate
    # regularly. NEVER commit real values. Stored token -> org.
    bootstrap_tokens = {
        (t.split(":", 1)[0] if ":" in t else t): (t.split(":", 1)[1] if ":" in t else "default")
        for t in os.getenv("BOOTSTRAP_TOKENS", "").split(",") if t
    }

    # BOOTSTRAP-ONLY admin key. Real auth is per-admin accounts (User.role=admin);
    # this key only works until the first admin account exists, then auth.py
    # retires it automatically. Default is empty = disabled: there is no built-in
    # credential, so an unconfigured deployment is closed, not wide open with a
    # publicly-known "dev-admin-key".
    admin_api_key = os.getenv("ADMIN_API_KEY", "")

    # Public URL of the panel/control plane, used to build the magic-link in the
    # sign-in email (e.g. https://fleet.example). If unset, the email carries just
    # the paste-in code (which is all the Mac app needs anyway).
    public_base_url = os.getenv("PUBLIC_BASE_URL", "")

    # A device is "offline" if it hasn't sent a heartbeat in this many seconds
    # (agent ticks every 15s).
    offline_after_s = int(os.getenv("OFFLINE_AFTER_S", "60"))

    # Alert thresholds.
    temp_alert_c = float(os.getenv("TEMP_ALERT_C", "75"))

    # ── Alert delivery (notifier.py). All optional; unset = that channel off.
    # With NOTHING set, alerts are still detected and shown in the panel — they
    # just aren't pushed. This is the walkthrough's "page you by email or webhook".
    alert_eval_interval_s = int(os.getenv("ALERT_EVAL_INTERVAL_S", "30"))
    alert_webhook_url = os.getenv("ALERT_WEBHOOK_URL", "")   # POST JSON here (Slack/Discord/n8n/…)
    alert_webhook_format = os.getenv("ALERT_WEBHOOK_FORMAT", "raw")  # slack | discord | raw
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_starttls = os.getenv("SMTP_STARTTLS", "1") not in ("0", "false", "no", "")
    alert_email_from = os.getenv("ALERT_EMAIL_FROM", "")
    alert_email_to = os.getenv("ALERT_EMAIL_TO", "")

    # Retention windows (see retention.py). Telemetry cannot go below 7 days
    # without also changing /admin/devices/{id}/uptime, which reads a rolling
    # 7-day window of raw ticks.
    telemetry_retention_days = int(os.getenv("TELEMETRY_RETENTION_DAYS", "7"))
    audit_retention_days = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
    command_retention_days = int(os.getenv("COMMAND_RETENTION_DAYS", "30"))

    # Telemetry rollup (rollup.py, walkthrough "rolled up after 48h"). Raw ticks
    # older than this collapse to one row per (device, hour); the hourly rollups
    # are then kept this many days. The uptime endpoint reads raw for the recent
    # window and rollups beyond it.
    rollup_raw_keep_hours = int(os.getenv("ROLLUP_RAW_KEEP_HOURS", "48"))
    rollup_retention_days = int(os.getenv("ROLLUP_RETENTION_DAYS", "90"))


settings = Settings()
