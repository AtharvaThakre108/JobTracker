# app/routes/jobs.py
# Minimal version for now — full scraper routes in Round 11
from flask import Blueprint
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.utils import success, error
from app.extensions import limiter

jobs_bp = Blueprint("jobs", __name__, url_prefix="/api/jobs")


@jobs_bp.route("/auto-apply/trigger", methods=["POST"])
@jwt_required()
@limiter.limit("3 per hour")
def trigger_auto_apply():
    """
    Manually trigger the autonomous apply task for the current user.
    Normally this runs on a schedule — this endpoint is for testing.
    """
    user_id: str = get_jwt_identity()

    from app.tasks.apply_tasks import run_autonomous_apply
    task = run_autonomous_apply.delay(user_id)

    return success(
        data={"task_id": task.id},
        message="Auto-apply task queued. Check notifications for results.",
        status_code=202,
    )


@jobs_bp.route("/auto-apply/status/<task_id>", methods=["GET"])
@jwt_required()
def auto_apply_status(task_id: str):
    """Check the status of a queued auto-apply task."""
    from wsgi import celery_app
    task = celery_app.AsyncResult(task_id)

    return success(data={
        "task_id": task_id,
        "state":   task.state,
        "result":  task.result if task.ready() else None,
    })