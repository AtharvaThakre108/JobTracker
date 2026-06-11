# app/config.py
# ─────────────────────────────────────────────────────────────────────────────
# Central configuration for all environments.
# Never import this directly in routes/services — always use current_app.config
# ─────────────────────────────────────────────────────────────────────────────

import os
from datetime import timedelta
from dotenv import load_dotenv

# Load .env file into os.environ before anything reads it
load_dotenv()


class Config:
    """
    Base config — shared across all environments.
    Child classes override only what differs.
    """

    # ── App ───────────────────────────────────────────────────
    APP_NAME: str = os.environ.get("APP_NAME", "JobTracker Pro")
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    FRONTEND_URL: str = os.environ.get("FRONTEND_URL", "http://localhost:5173")

    # ── Database ──────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI: str = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/jobtracker"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False   # Suppress deprecation warning
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": True,   # Test connection before using it (handles dropped connections)
        "pool_recycle": 300,     # Recycle connections every 5 min (prevents stale conn errors)
        "pool_size": 10,         # Max persistent connections in pool
        "max_overflow": 20,      # Extra connections allowed under heavy load
    }

    # ── JWT ───────────────────────────────────────────────────
    JWT_SECRET_KEY: str = os.environ.get("JWT_SECRET_KEY", "jwt-secret-change-in-prod")
    JWT_ACCESS_TOKEN_EXPIRES: timedelta = timedelta(
        minutes=int(os.environ.get("JWT_ACCESS_TOKEN_EXPIRES_MINUTES", 15))
    )
    JWT_REFRESH_TOKEN_EXPIRES: timedelta = timedelta(
        days=int(os.environ.get("JWT_REFRESH_TOKEN_EXPIRES_DAYS", 7))
    )
    JWT_TOKEN_LOCATION: list = ["headers"]
    JWT_HEADER_NAME: str = "Authorization"
    JWT_HEADER_TYPE: str = "Bearer"

    # ── Redis ─────────────────────────────────────────────────
    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # ── Celery (uses Redis as broker) ─────────────────────────
    CELERY_BROKER_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CELERY_TASK_SERIALIZER: str = "json"
    CELERY_RESULT_SERIALIZER: str = "json"
    CELERY_ACCEPT_CONTENT: list = ["json"]
    CELERY_TIMEZONE: str = "Asia/Kolkata"

    # ── Rate Limiting ─────────────────────────────────────────
    # Stored in Redis so limits persist across server restarts
    RATELIMIT_STORAGE_URI: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    RATELIMIT_DEFAULT: str = "200 per hour;20 per minute"
    RATELIMIT_HEADERS_ENABLED: bool = True    # Send X-RateLimit-* headers to client

    RAPIDAPI_KEY: str = os.environ.get("RAPIDAPI_KEY", "")

    # ── LinkedIn OAuth ────────────────────────────────────────
    LINKEDIN_CLIENT_ID: str = os.environ.get("LINKEDIN_CLIENT_ID", "")
    LINKEDIN_CLIENT_SECRET: str = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
    LINKEDIN_REDIRECT_URI: str = os.environ.get(
        "LINKEDIN_REDIRECT_URI",
        "http://localhost:5000/api/auth/linkedin/callback"
    )
    LINKEDIN_AUTH_URL: str = "https://www.linkedin.com/oauth/v2/authorization"
    LINKEDIN_TOKEN_URL: str = "https://www.linkedin.com/oauth/v2/accessToken"
    LINKEDIN_USERINFO_URL: str = "https://api.linkedin.com/v2/userinfo"

    # ── Email ─────────────────────────────────────────────────
    SENDGRID_API_KEY: str = os.environ.get("SENDGRID_API_KEY", "")
    FROM_EMAIL: str = os.environ.get("FROM_EMAIL", "noreply@jobtracker.app")
    FROM_NAME: str = os.environ.get("FROM_NAME", "JobTracker Pro")

    # ── AWS S3 ────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.environ.get("AWS_REGION", "ap-south-1")
    S3_BUCKET_NAME: str = os.environ.get("S3_BUCKET_NAME", "jobtracker-resumes")
    MAX_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024    # 10 MB

    # ── Encryption ────────────────────────────────────────────
    # Fernet key — used to encrypt TOTP secrets and OAuth tokens before storing in DB
    ENCRYPTION_KEY: str = os.environ.get("ENCRYPTION_KEY", "")

    # ── Inbound Email ─────────────────────────────────────────
    INBOUND_EMAIL_DOMAIN: str = os.environ.get("INBOUND_EMAIL_DOMAIN", "mail.jobtracker.app")

    # ── CORS ──────────────────────────────────────────────────
    # Only these origins are allowed to call the API
    CORS_ORIGINS: list = [
        "http://localhost:5173",       # Vite dev server
        "http://localhost:3000",       # Alternate dev port
        os.environ.get("FRONTEND_URL", ""),
    ]


# ─────────────────────────────────────────────────────────────────────────────

class DevelopmentConfig(Config):
    """Local dev — verbose errors, no HTTPS requirement."""
    DEBUG: bool = True
    SQLALCHEMY_ECHO: bool = False    # Flip to True to log every SQL query (noisy but useful)


class ProductionConfig(Config):
    """Railway / any cloud host — strict security settings."""
    DEBUG: bool = False
    SQLALCHEMY_ECHO: bool = False

    # Tighter JWT settings in production
    JWT_COOKIE_SECURE: bool = True       # Only send JWT over HTTPS
    JWT_COOKIE_SAMESITE: str = "Lax"     # CSRF protection


class TestingConfig(Config):
    """pytest — isolated test DB, fast token expiry."""
    TESTING: bool = True
    SQLALCHEMY_DATABASE_URI: str = (
        "postgresql://postgres:postgres@localhost:5432/jobtracker_test"
    )
    JWT_ACCESS_TOKEN_EXPIRES: timedelta = timedelta(minutes=5)
    RATELIMIT_ENABLED: bool = False      # Disable rate limits during tests


# ─────────────────────────────────────────────────────────────────────────────
# Lookup map — used by the app factory:
#   app.config.from_object(config[os.environ.get("FLASK_ENV", "development")])
# ─────────────────────────────────────────────────────────────────────────────

config: dict = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}