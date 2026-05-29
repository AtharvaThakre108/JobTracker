# app/__init__.py
# ─────────────────────────────────────────────────────────────────────────────
# Application factory.
#
# WHY a factory instead of a global app instance:
#   - Different configs per environment (dev / prod / test)
#   - Multiple instances in tests without state leaking between them
#   - Extensions initialised once, safely, without circular imports
#
# FLOW:
#   wsgi.py → create_app("development")
#           → load config
#           → init extensions
#           → register JWT error handlers
#           → register blueprints (routes)
#           → register HTTP error handlers
#           → return app
# ─────────────────────────────────────────────────────────────────────────────

import os
from flask import Flask, jsonify

from app.config import config
from app.extensions import db, migrate, jwt, cors, limiter


def create_app(config_name: str = "default") -> Flask:
    """
    Create and configure a Flask application instance.

    Args:
        config_name: One of "development", "production", "testing", "default".
                     Loaded from FLASK_ENV in .env automatically via wsgi.py.

    Returns:
        Flask: Fully configured application ready to serve requests.
    """

    app = Flask(__name__)

    # ── Load config ───────────────────────────────────────────────────────────
    # Pulls all settings from the matching Config class in config.py
    app.config.from_object(config[config_name])

    # ── Initialise extensions ─────────────────────────────────────────────────
    # Each extension was created in extensions.py WITHOUT an app.
    # init_app() binds them to THIS app instance.
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    limiter.init_app(app)
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=True,
    )

    # ── JWT event handlers ────────────────────────────────────────────────────
    # These replace JWT's default HTML error pages with clean JSON responses.

    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(jwt_header: dict, jwt_payload: dict) -> bool:
        """
        Called automatically on every protected route.
        Returns True if this token has been revoked (logged out).
        """
        from app.routes.auth import is_token_blocked
        return is_token_blocked(jwt_payload["jti"])

    @jwt.revoked_token_loader
    def revoked_token_response(jwt_header: dict, jwt_payload: dict):
        return jsonify({
            "success": False,
            "message": "Token revoked. Please log in again.",
            "code": "token_revoked",
        }), 401

    @jwt.expired_token_loader
    def expired_token_response(jwt_header: dict, jwt_payload: dict):
        return jsonify({
            "success": False,
            "message": "Token has expired.",
            "code": "token_expired",   # Frontend uses this to trigger refresh
        }), 401

    @jwt.unauthorized_loader
    def missing_token_response(reason: str):
        return jsonify({
            "success": False,
            "message": "Authentication required.",
            "code": "missing_token",
        }), 401

    @jwt.invalid_token_loader
    def invalid_token_response(reason: str):
        return jsonify({
            "success": False,
            "message": "Invalid token.",
            "code": "invalid_token",
        }), 422

    # ── Register blueprints ───────────────────────────────────────────────────
    # Each blueprint is a group of related routes defined in its own file.
    # We import here (inside the function) to avoid circular imports.

    from app.routes.auth import auth_bp
    app.register_blueprint(auth_bp)

    # app/__init__.py — add after auth_bp registration
    from app.routes.applications import applications_bp
    app.register_blueprint(applications_bp)

    from app.routes.analytics import analytics_bp
    app.register_blueprint(analytics_bp)

    from app.routes.resume import resume_bp
    app.register_blueprint(resume_bp)

    from app.routes.ml import ml_bp
    app.register_blueprint(ml_bp)
    # The remaining blueprints are registered as we build them in later rounds.
    # Uncomment each one when its file is ready:

    # from app.routes.applications import applications_bp
    # app.register_blueprint(applications_bp)

    # from app.routes.analytics import analytics_bp
    # app.register_blueprint(analytics_bp)

    # from app.routes.resume import resume_bp
    # app.register_blueprint(resume_bp)

    # from app.routes.jobs import jobs_bp
    # app.register_blueprint(jobs_bp)

    # from app.routes.ml import ml_bp
    # app.register_blueprint(ml_bp)

    # from app.routes.notifications import notifications_bp
    # app.register_blueprint(notifications_bp)

    # from app.routes.user import user_bp
    # app.register_blueprint(user_bp)

    # ── Health check ──────────────────────────────────────────────────────────
    # Used by Railway and monitoring tools to confirm the server is alive.
    # No auth required — just returns 200 OK.

    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "app": app.config["APP_NAME"],
            "env": config_name,
        }), 200

    # ── HTTP error handlers ───────────────────────────────────────────────────
    # Replace Flask's default HTML error pages with JSON (consistent with API).

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"success": False, "message": "Resource not found."}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"success": False, "message": "Method not allowed."}), 405

    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        return jsonify({
            "success": False,
            "message": "Too many requests. Please slow down.",
            "code": "rate_limited",
        }), 429

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.error(f"Internal server error: {e}")
        return jsonify({"success": False, "message": "Internal server error."}), 500

    return app