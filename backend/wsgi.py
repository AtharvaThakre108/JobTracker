# wsgi.py
# ─────────────────────────────────────────────────────────────────────────────
# Application entry point.
#
# LOCAL DEV (Windows):
#   python wsgi.py
#   → starts Flask on http://localhost:5000
#
# PRODUCTION (Railway):
#   Railway detects wsgi.py automatically.
#   Set start command to: waitress-serve --host=0.0.0.0 --port=5000 wsgi:app
#
# NOTE: Never use Flask's built-in server (app.run) in production.
#   Flask's dev server is single-threaded and not safe for real traffic.
#   Waitress is a production-grade WSGI server that works on Windows.
# ─────────────────────────────────────────────────────────────────────────────

import os
from app import create_app

# Create the Flask app using the environment specified in .env
# Falls back to "development" if FLASK_ENV is not set
app = create_app(os.environ.get("FLASK_ENV", "development"))


if __name__ == "__main__":
    env: str = os.environ.get("FLASK_ENV", "development")

    if env == "production":
        # Production — use Waitress (Windows-compatible WSGI server)
        from waitress import serve
        print(f"[JobTracker] Starting production server on port 5000...")
        serve(app, host="0.0.0.0", port=5000)
    else:
        # Development — use Flask's built-in server (auto-reload on code change)
        print(f"[JobTracker] Starting dev server → http://localhost:5000")
        app.run(
            host="0.0.0.0",
            port=5000,
            debug=True,
            use_reloader=True,    # Restart server automatically on file save
        )