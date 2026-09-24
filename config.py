"""
Chirix-to-Tally Middleware — Application Configuration
=======================================================
Centralised configuration for Development, Production, and Testing environments.
All secrets and tuneable values are read from environment variables.
"""

import os


class BaseConfig:
    """Shared configuration defaults."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # ── Tally connection ─────────────────────────────────────────────────────
    # Direct Tally HTTP URL (used in LOCAL mode when the server runs on the
    # same machine as TallyPrime).
    TALLY_HTTP_URL = os.environ.get("TALLY_HTTP_URL", "http://127.0.0.1:9000")

    # Target a specific company inside TallyPrime.  Leave blank ("") to use
    # whichever company is currently active.
    TALLY_COMPANY_NAME = os.environ.get("TALLY_COMPANY_NAME", "")

    # ── Tally Connector (CLOUD mode) ─────────────────────────────────────────
    # When the Flask app is hosted on a cloud server (e.g. Render), it cannot
    # reach localhost:9000 on the user's PC.  Set TALLY_CONNECTOR_URL to the
    # public URL of the lightweight local agent running on the user's Windows
    # machine.  When this is set, /api/push and /api/status will proxy through
    # the connector instead of calling Tally directly.
    #
    # Example: TALLY_CONNECTOR_URL=https://abc123.loca.lt
    TALLY_CONNECTOR_URL = os.environ.get("TALLY_CONNECTOR_URL", "")

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins.  "*" permits all (dev default).
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")

    # ── Chirix ERP API ───────────────────────────────────────────────────────
    CHIRIX_API_URL = os.environ.get(
        "CHIRIX_API_URL", "https://www.chirixsolutions.com/api/data"
    )

    # ── Logging ──────────────────────────────────────────────────────────────
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


class DevelopmentConfig(BaseConfig):
    """Local development — debug ON, verbose logging."""
    DEBUG = True
    LOG_LEVEL = "DEBUG"


class ProductionConfig(BaseConfig):
    """Render / cloud — debug OFF, no file logging."""
    DEBUG = False
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


class TestingConfig(BaseConfig):
    """Automated test runs."""
    TESTING = True
    DEBUG = True
    LOG_LEVEL = "DEBUG"


# ── Config selector ──────────────────────────────────────────────────────────
_config_map = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "testing":     TestingConfig,
}


def get_config():
    """Return the configuration class for the current FLASK_ENV."""
    env = os.environ.get("FLASK_ENV", "production").lower()
    return _config_map.get(env, ProductionConfig)
