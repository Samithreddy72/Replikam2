"""Control-plane configuration, all from environment.

Single-org for now; ORG_ID is reserved in the schema for future multi-tenant.
"""
import os


class Settings:
    # SQLite by default for local dev; point at Postgres in production, e.g.
    #   postgresql+psycopg://bridge:bridge@localhost/bridge
    database_url = os.getenv("DATABASE_URL", "sqlite:///./bridge.db")

    # Comma-separated set of valid one-time-ish bootstrap tokens that flashed Pis
    # present at first enrollment. Rotate regularly. NEVER commit real values.
    bootstrap_tokens = [t for t in os.getenv("BOOTSTRAP_TOKENS", "").split(",") if t]

    # BOOTSTRAP-ONLY admin key. Real auth is per-admin accounts (User.role=admin);
    # this key only works until the first admin account exists, then auth.py
    # retires it automatically. Default is empty = disabled: there is no built-in
    # credential, so an unconfigured deployment is closed, not wide open with a
    # publicly-known "dev-admin-key".
    admin_api_key = os.getenv("ADMIN_API_KEY", "")

    # A device is "offline" if it hasn't sent a heartbeat in this many seconds
    # (agent ticks every 15s).
    offline_after_s = int(os.getenv("OFFLINE_AFTER_S", "60"))

    # Alert thresholds.
    temp_alert_c = float(os.getenv("TEMP_ALERT_C", "75"))

    # Retention windows (see retention.py). Telemetry cannot go below 7 days
    # without also changing /admin/devices/{id}/uptime, which reads a rolling
    # 7-day window of raw ticks.
    telemetry_retention_days = int(os.getenv("TELEMETRY_RETENTION_DAYS", "7"))
    audit_retention_days = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
    command_retention_days = int(os.getenv("COMMAND_RETENTION_DAYS", "30"))


settings = Settings()
