# wsgi.py — replace entire file with this

import os
from app import create_app
from app.tasks.celery_app import celery_init_app

app = create_app(os.environ.get("FLASK_ENV", "development"))

# Create the properly configured Celery instance
celery_app = celery_init_app(app)

if __name__ == "__main__":
    env: str = os.environ.get("FLASK_ENV", "development")
    if env == "production":
        from waitress import serve
        print("[JobTracker] Starting production server on port 5000...")
        serve(app, host="0.0.0.0", port=5000)
    else:
        print("[JobTracker] Starting dev server → http://localhost:5000")
        app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True)