# app/routes/ml.py
# ─────────────────────────────────────────────────────────────────────────────
# ML feature endpoints.
#
# ENDPOINTS:
#   POST /api/ml/ats-score       — score resume vs a job description
#   GET  /api/ml/skillgap        — what skills am I missing for a role?
#   GET  /api/ml/suggestions     — rank saved applications by match score
#   POST /api/ml/match           — score a single resume vs JD (no DB needed)
# ─────────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional

from flask import Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db, limiter
from app.models import Resume, JobApplication
from app.utils import success, error

logger = logging.getLogger(__name__)

ml_bp = Blueprint("ml", __name__, url_prefix="/api/ml")


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/ml/ats-score
# ─────────────────────────────────────────────────────────────────────────────

@ml_bp.route("/ats-score", methods=["POST"])
@jwt_required()
@limiter.limit("30 per hour")
def ats_score():
    """
    Score the user's active resume against a job description.

    Body: { "job_description": str }

    Returns ATS score (0–100) with present/missing keywords.
    Optionally saves the score to an existing application.

    Body (optional): { "application_id": str }
    """
    user_id: str = get_jwt_identity()
    data: dict   = request.get_json(silent=True) or {}

    jd: str = (data.get("job_description") or "").strip()
    if not jd:
        return error("job_description is required.", 422)

    # ── Get active resume ─────────────────────────────────────────────────────
    resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first()

    if not resume or not resume.parsed_data:
        return error("No active resume found. Upload a resume first.", 404)

    resume_text: str = resume.parsed_data.get("raw_text", "")

    # ── Compute ATS score ─────────────────────────────────────────────────────
    from app.services.ats_optimizer import compute_ats_score
    result: dict = compute_ats_score(resume_text, jd)

    # ── Optionally save score to an application ───────────────────────────────
    app_id: Optional[str] = data.get("application_id")
    if app_id:
        app = JobApplication.query.filter_by(
            id=app_id, user_id=user_id
        ).first()
        if app:
            app.ats_score = result["score"]
            db.session.commit()

    return success(data={"ats": result})


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/ml/skillgap
# ─────────────────────────────────────────────────────────────────────────────

@ml_bp.route("/skillgap", methods=["POST"])
@jwt_required()
@limiter.limit("30 per hour")
def skillgap():
    """
    Find which skills are missing from the resume for a given JD.

    Body: { "job_description": str }

    Returns:
        present  — skills you have that match
        missing  — skills in JD but not in your resume
        coverage — % of JD skills covered
    """
    user_id: str = get_jwt_identity()
    data: dict   = request.get_json(silent=True) or {}

    jd: str = (data.get("job_description") or "").strip()
    if not jd:
        return error("job_description is required.", 422)

    resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first()

    if not resume:
        return error("No active resume found.", 404)

    # ── Compute skill gap ─────────────────────────────────────────────────────
    from app.ml.job_matcher import get_skill_gap
    gap: dict = get_skill_gap(resume.skills or [], jd)

    return success(data={"skillgap": gap})


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/ml/suggestions
# ─────────────────────────────────────────────────────────────────────────────

@ml_bp.route("/suggestions", methods=["GET"])
@jwt_required()
@limiter.limit("20 per hour")
def suggestions():
    """
    Score and rank the user's saved applications by resume match.

    Uses the active resume to compute match scores for all applications
    that have a job_description stored but no match_score yet.

    Returns applications sorted by match_score descending.
    """
    user_id: str = get_jwt_identity()

    resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first()

    if not resume or not resume.parsed_data:
        return error("No active resume found.", 404)

    resume_text:   str       = resume.parsed_data.get("raw_text", "")
    resume_skills: list[str] = resume.skills or []

    # ── Get applications with JDs ─────────────────────────────────────────────
    apps = JobApplication.query.filter(
        JobApplication.user_id == user_id,
        JobApplication.job_description.isnot(None),
    ).all()

    if not apps:
        return success(data={
            "suggestions": [],
            "message": "Add job descriptions to your applications to get match scores.",
        })

    # ── Score each application ────────────────────────────────────────────────
    from app.ml.job_matcher import score_match

    results: list[dict] = []
    for app in apps:
        try:
            score: float = score_match(
                resume_text,
                resume_skills,
                app.job_description,
            )
            # Save score back to DB for future reference
            if app.match_score is None:
                app.match_score = score

            results.append({
                "application_id": app.id,
                "company_name":   app.company_name,
                "role":           app.role,
                "status":         app.status,
                "match_score":    score,
                "match_pct":      f"{round(score * 100, 1)}%",
            })
        except Exception as e:
            logger.warning(f"Scoring failed for {app.id}: {e}")

    db.session.commit()

    # Sort best matches first
    results.sort(key=lambda x: x["match_score"], reverse=True)

    return success(data={
        "suggestions": results,
        "total":       len(results),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/ml/match
# ─────────────────────────────────────────────────────────────────────────────

@ml_bp.route("/match", methods=["POST"])
@jwt_required()
@limiter.limit("30 per hour")
def match():
    """
    Score the active resume against any job description on the fly.
    No DB storage — pure computation. Used by the job search UI
    to show a match score before the user decides to apply.

    Body: { "job_description": str }

    Returns:
        match_score  — 0.0 to 1.0
        match_pct    — "76.3%"
        skillgap     — present/missing skills
        ats          — ATS keyword score
    """
    user_id: str = get_jwt_identity()
    data: dict   = request.get_json(silent=True) or {}

    jd: str = (data.get("job_description") or "").strip()
    if not jd:
        return error("job_description is required.", 422)

    resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first()

    if not resume or not resume.parsed_data:
        return error("No active resume found.", 404)

    resume_text:   str       = resume.parsed_data.get("raw_text", "")
    resume_skills: list[str] = resume.skills or []

    from app.ml.job_matcher import score_match, get_skill_gap
    from app.services.ats_optimizer import compute_ats_score

    try:
        match_score: float = score_match(resume_text, resume_skills, jd)
        gap: dict          = get_skill_gap(resume_skills, jd)
        ats: dict          = compute_ats_score(resume_text, jd)
    except RuntimeError as e:
        # Model failed to load
        return error(str(e), 503)

    return success(data={
        "match_score": match_score,
        "match_pct":   f"{round(match_score * 100, 1)}%",
        "skillgap":    gap,
        "ats":         ats,
    })