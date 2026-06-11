# app/routes/applications.py
# ─────────────────────────────────────────────────────────────────────────────
# Job application CRUD endpoints.
#
# ENDPOINTS:
#   GET    /api/applications            — list applications (filters + pagination)
#   POST   /api/applications            — create new application
#   GET    /api/applications/<id>       — get single application + full timeline
#   PUT    /api/applications/<id>       — update application
#   DELETE /api/applications/<id>       — delete application
#   GET    /api/applications/export     — download all as .xlsx
#
# RULES:
#   - Every route requires a valid JWT token
#   - Users can only access their own applications (filtered by user_id)
#   - Every status change is logged to status_history automatically
#   - Deleting an application cascades to status_history, cover_letters,
#     interview_sessions (defined in the model relationships)
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, request, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db, limiter
from app.models import (
    JobApplication, StatusHistory, Notification,
    AppStatus, AppSource, CompanyType, NotifType
)
from app.utils import success, error, log_action, paginate

applications_bp = Blueprint("applications", __name__, url_prefix="/api/applications")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/applications
# ─────────────────────────────────────────────────────────────────────────────

@applications_bp.route("", methods=["GET"])
@jwt_required()
def list_applications():
    """
    Return a paginated list of the current user's applications.

    Query params (all optional):
        status        — filter by status e.g. ?status=Interview
        company_type  — filter by company type e.g. ?company_type=Startup
        source        — filter by source e.g. ?source=LinkedIn
        search        — search company name or role e.g. ?search=google
        sort          — field to sort by (default: applied_date)
        order         — asc or desc (default: desc)
        page          — page number (default: 1)
        per_page      — results per page (default: 20, max: 100)
    """
    user_id: str = get_jwt_identity()

    # ── Base query — always scoped to current user ────────────────────────────
    query = JobApplication.query.filter_by(user_id=user_id)

    # ── Filters ───────────────────────────────────────────────────────────────
    status: Optional[str] = request.args.get("status")
    if status and status in AppStatus.ALL:
        query = query.filter_by(status=status)

    company_type: Optional[str] = request.args.get("company_type")
    if company_type and company_type in CompanyType.ALL:
        query = query.filter_by(company_type=company_type)

    source: Optional[str] = request.args.get("source")
    if source and source in AppSource.ALL:
        query = query.filter_by(source=source)

    search: Optional[str] = request.args.get("search", "").strip()
    if search:
        # Case-insensitive search across company name and role
        pattern: str = f"%{search}%"
        query = query.filter(
            db.or_(
                JobApplication.company_name.ilike(pattern),
                JobApplication.role.ilike(pattern),
            )
        )

    # ── Sorting ───────────────────────────────────────────────────────────────
    sort_field: str  = request.args.get("sort", "applied_date")
    sort_order: str  = request.args.get("order", "desc")

    # Whitelist sortable fields — never pass raw user input to order_by
    sortable: dict = {
        "applied_date":  JobApplication.applied_date,
        "company_name":  JobApplication.company_name,
        "role":          JobApplication.role,
        "status":        JobApplication.status,
        "match_score":   JobApplication.match_score,
        "created_at":    JobApplication.created_at,
    }

    sort_column = sortable.get(sort_field, JobApplication.applied_date)
    query = query.order_by(
        sort_column.desc() if sort_order == "desc" else sort_column.asc()
    )

    # ── Paginate ──────────────────────────────────────────────────────────────
    items, meta = paginate(query)

    return success(data={
        "applications": [_serialize(app) for app in items],
        "pagination":   meta,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/applications
# ─────────────────────────────────────────────────────────────────────────────

@applications_bp.route("", methods=["POST"])
@jwt_required()
@limiter.limit("100 per day")   # Prevent accidental bulk spam
def create_application():
    """
    Create a new job application.

    Body (required): company_name, role
    Body (optional): company_type, job_url, job_description, source,
                     status, applied_date, location, is_remote,
                     salary_target, notes

    Also creates the first StatusHistory entry for the timeline.
    """
    user_id: str = get_jwt_identity()
    data: dict   = request.get_json(silent=True) or {}

    # ── Validate required fields ──────────────────────────────────────────────
    company_name: str = (data.get("company_name") or "").strip()
    role: str         = (data.get("role")         or "").strip()

    field_errors: dict = {}
    if not company_name:
        field_errors["company_name"] = "Company name is required."
    if not role:
        field_errors["role"] = "Role is required."
    if field_errors:
        return error("Validation failed.", 422, field_errors)

    # ── Parse applied_date ────────────────────────────────────────────────────
    applied_date = datetime.now(timezone.utc)
    if data.get("applied_date"):
        try:
            applied_date = datetime.fromisoformat(data["applied_date"])
        except ValueError:
            return error("Invalid applied_date format. Use ISO 8601.", 422)

    # ── Create application ────────────────────────────────────────────────────
    app = JobApplication(
        user_id=user_id,
        company_name=company_name,
        role=role,
        company_type=data.get("company_type"),
        job_url=data.get("job_url"),
        job_description=data.get("job_description"),
        source=data.get("source", AppSource.MANUAL),
        status=data.get("status", AppStatus.APPLIED),
        applied_date=applied_date,
        location=data.get("location"),
        is_remote=bool(data.get("is_remote", False)),
        salary_target=data.get("salary_target"),
        notes=data.get("notes"),
        applied_by=data.get("applied_by", "user"),
    )
    db.session.add(app)
    db.session.flush()   # Get app.id before creating history entry

    # ── Write first status history entry ─────────────────────────────────────
    # old_status is None on creation — marks the start of the timeline
    db.session.add(StatusHistory(
        application_id=app.id,
        old_status=None,
        new_status=app.status,
        note="Application created.",
    ))

    db.session.commit()

    log_action(
        "application.created",
        entity_type="application",
        entity_id=app.id,
        user_id=user_id,
        new_value={"company": company_name, "role": role, "status": app.status},
    )

    return success(
        data={"application": _serialize(app)},
        message="Application added.",
        status_code=201,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/applications/<id>
# ─────────────────────────────────────────────────────────────────────────────

@applications_bp.route("/<string:app_id>", methods=["GET"])
@jwt_required()
def get_application(app_id: str):
    """
    Return a single application with its full status timeline,
    cover letters, and interview sessions.
    """
    app = _get_app_or_404(app_id)
    return success(data={"application": _serialize(app, full=True)})


# ─────────────────────────────────────────────────────────────────────────────
#  PUT /api/applications/<id>
# ─────────────────────────────────────────────────────────────────────────────

@applications_bp.route("/<string:app_id>", methods=["PUT"])
@jwt_required()
def update_application(app_id: str):
    """
    Update any fields on an application.

    If `status` changes, automatically:
      1. Writes a new StatusHistory row (preserves timeline)
      2. Creates an in-app notification
      3. Updates last_status_change timestamp

    Body: any subset of application fields to update.
    Special field: `status_note` — attached to the status history entry.
    """
    app  = _get_app_or_404(app_id)
    data: dict = request.get_json(silent=True) or {}

    # ── Handle status change separately ──────────────────────────────────────
    new_status: Optional[str] = data.get("status")

    if new_status and new_status != app.status:
        if new_status not in AppStatus.ALL:
            return error(f"Invalid status. Must be one of: {AppStatus.ALL}", 422)

        old_status: str = app.status

        # Write to timeline
        db.session.add(StatusHistory(
            application_id=app.id,
            old_status=old_status,
            new_status=new_status,
            note=data.get("status_note"),
        ))

        # Create in-app notification
        db.session.add(Notification(
            user_id=app.user_id,
            type=NotifType.STATUS_CHANGE,
            title="Application status updated",
            message=f"{app.company_name} — {app.role} moved from {old_status} to {new_status}.",
            link=f"/applications/{app.id}",
        ))

        app.status             = new_status
        app.last_status_change = datetime.now(timezone.utc)

        log_action(
            "application.status_changed",
            entity_type="application",
            entity_id=app.id,
            user_id=app.user_id,
            old_value={"status": old_status},
            new_value={"status": new_status},
        )

    from app.tasks.email_tasks import send_status_change_email
    send_status_change_email.delay(
            app.user_id, app.id, old_status, new_status
        )

    # ── Update remaining fields ───────────────────────────────────────────────
    # Only update fields that were actually sent in the request body
    updatable: list[str] = [
        "company_name", "company_type", "role", "job_url",
        "job_description", "source", "location", "is_remote",
        "salary_target", "notes", "interview_date",
    ]

    for field in updatable:
        if field in data:
            setattr(app, field, data[field])

    db.session.commit()

    return success(
        data={"application": _serialize(app, full=True)},
        message="Application updated.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE /api/applications/<id>
# ─────────────────────────────────────────────────────────────────────────────

@applications_bp.route("/<string:app_id>", methods=["DELETE"])
@jwt_required()
def delete_application(app_id: str):
    """
    Permanently delete an application and all related data.
    Cascades to: status_history, cover_letters, interview_sessions.
    """
    app = _get_app_or_404(app_id)

    company: str = app.company_name
    role: str    = app.role

    db.session.delete(app)
    db.session.commit()

    log_action(
        "application.deleted",
        entity_type="application",
        entity_id=app_id,
        user_id=get_jwt_identity(),
        old_value={"company": company, "role": role},
    )

    return success(message="Application deleted.")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/applications/export
# ─────────────────────────────────────────────────────────────────────────────

@applications_bp.route("/export", methods=["GET"])
@jwt_required()
@limiter.limit("10 per hour")   # Exporting is expensive — limit it
def export_applications():
    """
    Export all applications as a formatted .xlsx spreadsheet.
    Returns the file as a download attachment.
    """
    user_id: str = get_jwt_identity()

    apps = JobApplication.query\
        .filter_by(user_id=user_id)\
        .order_by(JobApplication.applied_date.desc())\
        .all()

    from app.services.exporter import build_xlsx
    xlsx_buffer = build_xlsx(apps)

    log_action("application.exported", user_id=user_id,
               new_value={"count": len(apps)})

    return send_file(
        xlsx_buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="jobtracker_applications.xlsx",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_app_or_404(app_id: str) -> JobApplication:
    """
    Fetch an application by ID, scoped to the current user.
    Returns 404 if not found or belongs to a different user.
    Scoping by user_id means user A can never access user B's data
    even if they guess the UUID.
    """
    user_id: str = get_jwt_identity()
    app = JobApplication.query.filter_by(
        id=app_id,
        user_id=user_id,
    ).first()

    if not app:
        from flask import abort
        abort(404, description="Application not found.")

    return app


def _serialize(app: JobApplication, full: bool = False) -> dict:
    """
    Convert a JobApplication to a dict for API responses.

    Args:
        app:  The JobApplication instance.
        full: If True, includes status_history, cover_letters,
              and interview_sessions. Used for the detail view.
    """
    data: dict = {
        "id":              app.id,
        "company_name":    app.company_name,
        "company_type":    app.company_type,
        "role":            app.role,
        "job_url":         app.job_url,
        "source":          app.source,
        "status":          app.status,
        "applied_date":    app.applied_date.isoformat() if app.applied_date else None,
        "interview_date":  app.interview_date.isoformat() if app.interview_date else None,
        "last_status_change": app.last_status_change.isoformat() if app.last_status_change else None,
        "match_score":     app.match_score,
        "ats_score":       app.ats_score,
        "salary_target":   app.salary_target,
        "salary_market_min": app.salary_market_min,
        "salary_market_max": app.salary_market_max,
        "salary_currency": app.salary_currency,
        "location":        app.location,
        "is_remote":       app.is_remote,
        "notes":           app.notes,
        "applied_by":      app.applied_by,
        "resume_version_id": app.resume_version_id,
        "created_at":      app.created_at.isoformat() if app.created_at else None,
        "updated_at":      app.updated_at.isoformat() if app.updated_at else None,
    }

    # Full detail view — includes relational data
    if full:
        data["timeline"] = [
            {
                "old_status": h.old_status,
                "new_status": h.new_status,
                "note":       h.note,
                "changed_at": h.changed_at.isoformat() if h.changed_at else None,
            }
            for h in sorted(app.status_history, key=lambda h: h.changed_at)
        ]
        data["cover_letters"] = [
            {
                "id":       cl.id,
                "tone":     cl.tone,
                "version":  cl.version,
                "was_sent": cl.was_sent,
                "created_at": cl.created_at.isoformat() if cl.created_at else None,
            }
            for cl in app.cover_letters
        ]
        data["interview_sessions"] = [
            {
                "id":            s.id,
                "mode":          s.mode,
                "overall_score": s.overall_score,
                "created_at":    s.created_at.isoformat() if s.created_at else None,
            }
            for s in app.interview_sessions
        ]

    return data