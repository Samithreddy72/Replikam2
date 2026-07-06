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

    # Admin API key guarding the /admin/* surface (the panel sends it as a Bearer
    # token). Replace with real user auth when multi-tenant.
    admin_api_key = os.getenv("ADMIN_API_KEY", "dev-admin-key")

    # A device is "offline" if it hasn't sent a heartbeat in this many seconds
    # (agent ticks every 15s).
    offline_after_s = int(os.getenv("OFFLINE_AFTER_S", "60"))

    # Alert thresholds.
    temp_alert_c = float(os.getenv("TEMP_ALERT_C", "75"))


settings = Settings()
