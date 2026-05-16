# app/extensions.py
# ─────────────────────────────────────────────────────────────────────────────
# Flask extension instances — created here WITHOUT an app attached.
# The app factory (app/__init__.py) calls .init_app(app) on each one.
#
# WHY this pattern:
#   If we did `db = SQLAlchemy(app)` inside __init__.py, then any file that
#   imports `db` would also import `app`, creating a circular import chain.
#   Separating creation from registration solves this completely.
# ─────────────────────────────────────────────────────────────────────────────

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS


# ── Database ORM ──────────────────────────────────────────────────────────────
# All models inherit from db.Model
# All queries use db.session
db: SQLAlchemy = SQLAlchemy()


# ── Database Migrations ───────────────────────────────────────────────────────
# Tracks schema changes like git tracks code changes
# Commands: flask db migrate → flask db upgrade
migrate: Migrate = Migrate()


# ── JWT Authentication ────────────────────────────────────────────────────────
# Handles access tokens, refresh tokens, blocklist checks
jwt: JWTManager = JWTManager()


# ── CORS ──────────────────────────────────────────────────────────────────────
# Allows the React frontend (different port) to call the Flask API
cors: CORS = CORS()


# ── Rate Limiter ──────────────────────────────────────────────────────────────
# Default key = IP address. Overridden per-route to key by JWT user ID instead,
# so limits are per user not per IP (fairer for users behind shared networks).
# Storage backend (Redis) is set in config.py via RATELIMIT_STORAGE_URI.
limiter: Limiter = Limiter(key_func=get_remote_address)