"""Control-plane configuration, all from environment.

Single-org for now; ORG_ID is reserved in the schema for future multi-tenant.
"""
import os



def _env(name, default=""):
    """Read an env var, treating EMPTY as absent.

    os.getenv(name, default) only falls back when the variable is UNSET. Container runtimes
    routinely pass variables through as empty strings - docker compose does exactly this for
    any key missing from .env - so `int(os.getenv("SMTP_PORT", "587"))` got "" and died with
    `invalid literal for int()`, taking the whole control plane down in a crash loop on a
    half-configured deployment. Empty means "not configured", so it must mean the default.
    """
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return int(default)


def _env_float(name, default):
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return float(default)


class Settings:
    # SQLite by default for local dev; point at Postgres in production, e.g.
    #   postgresql+psycopg://bridge:bridge@localhost/bridge
    database_url = _env("DATABASE_URL", "sqlite:///./bridge.db")

    # Bootstrap tokens flashed Pis present at first enrollment, as a comma list of
    # `token` or `token:org`. A bare token enrolls the device into org "default";
    # `token:acme` enrolls it directly into org "acme" (multi-tenant). Rotate
    # regularly. NEVER commit real values. Stored token -> org.
    bootstrap_tokens = {
        (t.split(":", 1)[0] if ":" in t else t): (t.split(":", 1)[1] if ":" in t else "default")
        for t in _env("BOOTSTRAP_TOKENS", "").split(",") if t
    }

    # BOOTSTRAP-ONLY admin key. Real auth is per-admin accounts (User.role=admin);
    # this key only works until the first admin account exists, then auth.py
    # retires it automatically. Default is empty = disabled: there is no built-in
    # credential, so an unconfigured deployment is closed, not wide open with a
    # publicly-known "dev-admin-key".
    admin_api_key = _env("ADMIN_API_KEY", "")

    # Public URL of the panel/control plane, used to build the magic-link in the
    # sign-in email (e.g. https://fleet.example). If unset, the email carries just
    # the paste-in code (which is all the Mac app needs anyway).
    public_base_url = _env("PUBLIC_BASE_URL", "")

    # ── Mesh (Tailscale) — phase 5 embedded-client auth (mesh.py). On sign-in the
    # control plane mints a SCOPED EPHEMERAL auth key via the Tailscale API so the
    # presenter app joins the private mesh itself — no user Tailscale install, no
    # shared admin key, no hardcoded 100.x IP. ts_api_key UNSET = feature off
    # (the /auth/mesh-key endpoint answers 503). NEVER commit a real key.
    ts_api_key = _env("TS_API_KEY", "")                # Tailscale API key / OAuth access token
    ts_tailnet = _env("TS_TAILNET", "")                # e.g. taile564ff.ts.net; "" -> the key's default tailnet
    ts_source_tag = _env("TS_SOURCE_TAG", "tag:nb-source")  # tag applied to app nodes; the tailnet ACL scopes it to bridges only
    # Tag applied to BRIDGES when claim mints their mesh key. Distinct from the source
    # tag on purpose: the tailnet ACL grants tag:source -> tag:bridge on the media and
    # control ports only, so a leaked presenter key can never impersonate a bridge.
    ts_bridge_tag = _env("TS_BRIDGE_TAG", "tag:bridge")
    ts_key_ttl_s = _env_int("TS_KEY_TTL_S", 600)    # how long the minted KEY is valid to join (seconds)
    ts_login_server = _env("TS_LOGIN_SERVER", "https://login.tailscale.com")  # control server (Headscale-friendly)

    # A device is "offline" if it hasn't sent a heartbeat in this many seconds
    # (agent ticks every 15s).
    offline_after_s = _env_int("OFFLINE_AFTER_S", 60)

    # Alert thresholds.
    temp_alert_c = _env_float("TEMP_ALERT_C", 75)

    # ── Alert delivery (notifier.py). All optional; unset = that channel off.
    # With NOTHING set, alerts are still detected and shown in the panel — they
    # just aren't pushed. This is the walkthrough's "page you by email or webhook".
    alert_eval_interval_s = _env_int("ALERT_EVAL_INTERVAL_S", 30)
    alert_webhook_url = _env("ALERT_WEBHOOK_URL", "")   # POST JSON here (Slack/Discord/n8n/…)
    alert_webhook_format = _env("ALERT_WEBHOOK_FORMAT", "raw")  # slack | discord | raw
    smtp_host = _env("SMTP_HOST", "")
    smtp_port = _env_int("SMTP_PORT", 587)
    smtp_user = _env("SMTP_USER", "")
    smtp_password = _env("SMTP_PASSWORD", "")
    smtp_starttls = _env("SMTP_STARTTLS", "1") not in ("0", "false", "no", "")
    alert_email_from = _env("ALERT_EMAIL_FROM", "")
    alert_email_to = _env("ALERT_EMAIL_TO", "")

    # Retention windows (see retention.py). Telemetry cannot go below 7 days
    # without also changing /admin/devices/{id}/uptime, which reads a rolling
    # 7-day window of raw ticks.
    telemetry_retention_days = _env_int("TELEMETRY_RETENTION_DAYS", 7)
    audit_retention_days = _env_int("AUDIT_RETENTION_DAYS", 365)
    command_retention_days = _env_int("COMMAND_RETENTION_DAYS", 30)

    # Telemetry rollup (rollup.py, walkthrough "rolled up after 48h"). Raw ticks
    # older than this collapse to one row per (device, hour); the hourly rollups
    # are then kept this many days. The uptime endpoint reads raw for the recent
    # window and rollups beyond it.
    rollup_raw_keep_hours = _env_int("ROLLUP_RAW_KEEP_HOURS", 48)
    rollup_retention_days = _env_int("ROLLUP_RETENTION_DAYS", 90)


settings = Settings()
