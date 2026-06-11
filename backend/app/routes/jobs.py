# app/routes/jobs.py
# Minimal version for now — full scraper routes in Round 11
from flask import Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.utils import success, error
from app.extensions import limiter

jobs_bp = Blueprint("jobs", __name__, url_prefix="/api/jobs")


@jobs_bp.route("/auto-apply/trigger", methods=["POST"])
@jwt_required()
@limiter.limit("20 per hour")
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

@jobs_bp.route("/search", methods=["GET"])
@jwt_required()
@limiter.limit("30 per hour")
def search_jobs():
    """
    Search for jobs using JSearch API.

    Query params:
        role      — job title / keywords (required)
        location  — city or country (default: India)
        remote    — "true" | "false" (default: false)
        posted    — "today" | "3days" | "week" | "month" (default: week)
    """
    from app.services.job_search_service import search_jobs as api_search
    from app.ml.job_matcher import score_match
    from app.models import Resume

    user_id: str = get_jwt_identity()

    role: str     = (request.args.get("role") or "").strip()
    location: str = (request.args.get("location") or "India").strip()
    posted: str   = request.args.get("posted", "week")

    if not role:
        return error("role query parameter is required.", 422)

    # Fetch jobs
    jobs: list[dict] = api_search(
        role=role,
        location=location,
        date_posted=posted,
    )

    if not jobs:
        return success(data={"jobs": [], "total": 0})

    # Score each job against user's active resume if available
    resume = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first()

    if resume and resume.parsed_data:
        resume_text:   str       = resume.parsed_data.get("raw_text", "")
        resume_skills: list[str] = resume.skills or []

        for job in jobs:
            try:
                job["match_score"] = score_match(
                    resume_text,
                    resume_skills,
                    job.get("description", ""),
                )
                job["match_pct"] = f"{round(job['match_score'] * 100, 1)}%"
            except Exception:
                job["match_score"] = 0.0
                job["match_pct"] = "N/A"

        # Sort best matches first
        jobs.sort(key=lambda j: j.get("match_score", 0), reverse=True)

    return success(data={
        "jobs":  jobs,
        "total": len(jobs),
    })