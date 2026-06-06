# app/routes/user.py
# ─────────────────────────────────────────────────────────────────────────────
# User profile and settings endpoints.
#
# ENDPOINTS:
#   GET    /api/user/profile        — full profile + settings
#   PUT    /api/user/settings       — update any settings fields
#   GET    /api/user/audit-log      — paginated activity history
#   GET    /api/user/export         — trigger GDPR data export
#   DELETE /api/user/account        — permanently delete account
# ─────────────────────────────────────────────────────────────────────────────

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, request, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db, limiter
from app.models import (
    User, UserSettings, AuditLog,
    JobApplication, Resume, Notification,
)
from app.utils import success, error, log_action, paginate

user_bp = Blueprint("user", __name__, url_prefix="/api/user")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/user/profile
# ─────────────────────────────────────────────────────────────────────────────

@user_bp.route("/profile", methods=["GET"])
@jwt_required()
def get_profile():
    """
    Return the full user profile including settings and resume summary.

    Used by:
        - Frontend on app load to restore session
        - Settings page to show current preferences
        - Onboarding to check completion state
    """
    user: User = _get_user()

    # Quick stats for profile page
    total_apps: int = JobApplication.query.filter_by(user_id=user.id).count()
    total_resumes: int = Resume.query.filter_by(user_id=user.id).count()

    active_resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user.id, is_active=True
    ).first()

    return success(data={
        "user": _serialize_user(user),
        "stats": {
            "total_applications": total_apps,
            "total_resumes":      total_resumes,
            "active_resume":      {
                "id":             active_resume.id,
                "label":          active_resume.label,
                "skills_count":   len(active_resume.skills or []),
                "version_number": active_resume.version_number,
            } if active_resume else None,
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
#  PUT /api/user/settings
# ─────────────────────────────────────────────────────────────────────────────

@user_bp.route("/settings", methods=["PUT"])
@jwt_required()
def update_settings():
    """
    Update user settings — partial update, only send fields to change.

    All fields are optional. Any field not sent keeps its current value.

    Updatable fields:
        theme                   — "light"|"dark"|"midnight"|"sepia"|"forest"
        target_roles            — ["Python Dev", "Backend Engineer"]
        target_company_types    — ["Startup", "MNC", "Business"]
        target_locations        — ["Bangalore", "Remote"]
        remote_preference       — "remote"|"hybrid"|"onsite"|"any"
        salary_min              — integer (INR)
        salary_max              — integer (INR)
        salary_currency         — "INR"|"USD" etc.
        auto_apply_enabled      — bool
        auto_apply_min_score    — float 0.0–1.0 (match threshold)
        auto_apply_daily_limit  — int (max applies per day)
        auto_apply_portals      — ["Indeed", "Naukri", "LinkedIn"]
        auto_apply_hour         — int 0–23 (hour to run bot)
        notify_status_change    — bool
        notify_interview_remind — bool
        notify_weekly_digest    — bool
        notify_bulk_done        — bool
        onboarding_complete     — bool
        onboarding_step         — int
    """
    user: User = _get_user()
    data: dict = request.get_json(silent=True) or {}

    if not data:
        return error("No settings provided.", 400)

    settings: UserSettings = user.settings
    if not settings:
        settings = UserSettings(user_id=user.id)
        db.session.add(settings)

    # ── Snapshot old values for audit log ─────────────────────────────────────
    old_values: dict = _serialize_settings(settings)

    # ── Updatable string fields ────────────────────────────────────────────────
    string_fields: list[str] = [
        "theme", "remote_preference", "salary_currency",
    ]
    for field in string_fields:
        if field in data:
            setattr(settings, field, data[field])

    # ── Updatable integer fields ───────────────────────────────────────────────
    int_fields: list[str] = [
        "salary_min", "salary_max",
        "auto_apply_daily_limit", "auto_apply_hour",
        "onboarding_step",
    ]
    for field in int_fields:
        if field in data:
            try:
                setattr(settings, field, int(data[field]))
            except (ValueError, TypeError):
                return error(f"'{field}' must be an integer.", 422)

    # ── Updatable float fields ─────────────────────────────────────────────────
    if "auto_apply_min_score" in data:
        try:
            score: float = float(data["auto_apply_min_score"])
            if not 0.0 <= score <= 1.0:
                return error("auto_apply_min_score must be between 0.0 and 1.0.", 422)
            settings.auto_apply_min_score = score
        except (ValueError, TypeError):
            return error("auto_apply_min_score must be a float.", 422)

    # ── Updatable boolean fields ───────────────────────────────────────────────
    bool_fields: list[str] = [
        "auto_apply_enabled",
        "notify_status_change", "notify_interview_remind",
        "notify_weekly_digest", "notify_bulk_done",
        "onboarding_complete",
    ]
    for field in bool_fields:
        if field in data:
            setattr(settings, field, bool(data[field]))

    # ── Updatable list fields ──────────────────────────────────────────────────
    list_fields: list[str] = [
        "target_roles", "target_company_types",
        "target_locations", "auto_apply_portals",
    ]
    for field in list_fields:
        if field in data:
            value = data[field]
            if not isinstance(value, list):
                return error(f"'{field}' must be a list.", 422)
            setattr(settings, field, value)

    # ── Validate auto_apply_portals ────────────────────────────────────────────
    valid_portals: set[str] = {"Indeed", "Naukri", "LinkedIn", "Internshala"}
    if settings.auto_apply_portals:
        invalid: list = [
            p for p in settings.auto_apply_portals
            if p not in valid_portals
        ]
        if invalid:
            return error(
                f"Invalid portals: {invalid}. "
                f"Valid options: {sorted(valid_portals)}", 422
            )

    db.session.commit()

    log_action(
        "user.settings_changed",
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
        old_value=old_values,
        new_value=_serialize_settings(settings),
    )

    return success(
        data={"settings": _serialize_settings(settings)},
        message="Settings updated.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/user/audit-log
# ─────────────────────────────────────────────────────────────────────────────

@user_bp.route("/audit-log", methods=["GET"])
@jwt_required()
def audit_log():
    """
    Return a paginated list of the user's activity history.

    Query params:
        action  — filter by action type e.g. ?action=auth.login
        page    — page number (default: 1)
        per_page — results per page (default: 20)

    Shows last 90 days only.
    """
    user: User = _get_user()

    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=90)

    query = AuditLog.query.filter(
        AuditLog.user_id == user.id,
        AuditLog.created_at >= since,
    ).order_by(AuditLog.created_at.desc())

    # Optional filter by action
    action_filter: Optional[str] = request.args.get("action")
    if action_filter:
        query = query.filter(AuditLog.action.ilike(f"%{action_filter}%"))

    items, meta = paginate(query)

    return success(data={
        "logs": [
            {
                "id":          log.id,
                "action":      log.action,
                "entity_type": log.entity_type,
                "entity_id":   log.entity_id,
                "old_value":   log.old_value,
                "new_value":   log.new_value,
                "ip_address":  log.ip_address,
                "created_at":  log.created_at.isoformat() if log.created_at else None,
            }
            for log in items
        ],
        "pagination": meta,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/user/export
# ─────────────────────────────────────────────────────────────────────────────

@user_bp.route("/export", methods=["GET"])
@jwt_required()
@limiter.limit("3 per day")     # Expensive — limit strictly
def export_data():
    """
    Export all user data as a ZIP file containing JSON files.

    Contents:
        profile.json         — user info + settings
        applications.json    — all job applications + status history
        resumes.json         — all resume metadata + parsed data
        cover_letters.json   — all cover letters
        audit_log.json       — last 90 days of activity
        notifications.json   — all notifications

    Generated in-memory — no temp files written to disk.
    """
    user: User = _get_user()

    log_action(
        "user.data_exported",
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
    )

    zip_buffer: io.BytesIO = _build_export_zip(user)

    filename: str = (
        f"jobtracker_export_{user.id[:8]}_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"
    )

    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE /api/user/account
# ─────────────────────────────────────────────────────────────────────────────

@user_bp.route("/account", methods=["DELETE"])
@jwt_required()
@limiter.limit("3 per day")
def delete_account():
    """
    Permanently delete the user's account and all associated data.

    Requires password confirmation for security.
    Body: { "password": str }

    CASCADE deletes in DB handle:
        settings, resumes, applications, notifications,
        audit_logs, backup_codes, calendar_connections
    """
    import bcrypt

    user: User = _get_user()
    data: dict = request.get_json(silent=True) or {}
    password: str = data.get("password", "")

    # ── Verify password before deletion ──────────────────────────────────────
    if user.password_hash:
        if not password:
            return error("Password confirmation required.", 400)
        if not bcrypt.checkpw(
            password.encode("utf-8"),
            user.password_hash.encode("utf-8")
        ):
            return error("Incorrect password.", 401)

    user_id: str = user.id

    # Log before deleting — after deletion the user_id FK won't exist
    log_action(
        "user.account_deleted",
        entity_type="user",
        entity_id=user_id,
        user_id=user_id,
    )

    db.session.delete(user)
    db.session.commit()

    return success(message="Account permanently deleted.")


# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_user() -> User:
    """Fetch current user from JWT identity. Aborts 404 if not found."""
    user_id: str = get_jwt_identity()
    user: Optional[User] = db.session.get(User, user_id)
    if not user:
        from flask import abort
        abort(404, description="User not found.")
    return user


def _build_export_zip(user: User) -> io.BytesIO:
    """
    Build a ZIP archive containing all user data as JSON files.

    Args:
        user: The User instance to export data for.

    Returns:
        io.BytesIO: In-memory ZIP buffer seeked to position 0.
    """
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

        # ── profile.json ──────────────────────────────────────────────────────
        profile_data: dict = {
            "id":           user.id,
            "name":         user.name,
            "email":        user.email,
            "created_at":   user.created_at.isoformat() if user.created_at else None,
            "is_verified":  user.is_verified,
            "has_linkedin": bool(user.linkedin_id),
            "settings":     _serialize_settings(user.settings) if user.settings else {},
        }
        zf.writestr("profile.json", json.dumps(profile_data, indent=2))

        # ── applications.json ─────────────────────────────────────────────────
        apps = JobApplication.query.filter_by(user_id=user.id).all()
        apps_data: list[dict] = []
        for app in apps:
            app_dict: dict = {
                "id":           app.id,
                "company_name": app.company_name,
                "company_type": app.company_type,
                "role":         app.role,
                "job_url":      app.job_url,
                "source":       app.source,
                "status":       app.status,
                "applied_date": app.applied_date.isoformat() if app.applied_date else None,
                "match_score":  app.match_score,
                "ats_score":    app.ats_score,
                "salary_target": app.salary_target,
                "location":     app.location,
                "is_remote":    app.is_remote,
                "notes":        app.notes,
                "applied_by":   app.applied_by,
                "status_history": [
                    {
                        "old_status": h.old_status,
                        "new_status": h.new_status,
                        "note":       h.note,
                        "changed_at": h.changed_at.isoformat() if h.changed_at else None,
                    }
                    for h in app.status_history
                ],
            }
            apps_data.append(app_dict)
        zf.writestr("applications.json", json.dumps(apps_data, indent=2))

        # ── resumes.json ──────────────────────────────────────────────────────
        resumes = Resume.query.filter_by(user_id=user.id).all()
        resumes_data: list[dict] = [
            {
                "id":             r.id,
                "version_number": r.version_number,
                "label":          r.label,
                "file_name":      r.file_name,
                "skills":         r.skills,
                "is_active":      r.is_active,
                "created_at":     r.created_at.isoformat() if r.created_at else None,
                "parsed_data":    r.parsed_data,
            }
            for r in resumes
        ]
        zf.writestr("resumes.json", json.dumps(resumes_data, indent=2))

        # ── notifications.json ────────────────────────────────────────────────
        notifs = Notification.query.filter_by(user_id=user.id).all()
        notifs_data: list[dict] = [
            {
                "id":         n.id,
                "type":       n.type,
                "title":      n.title,
                "message":    n.message,
                "is_read":    n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notifs
        ]
        zf.writestr("notifications.json", json.dumps(notifs_data, indent=2))

        # ── audit_log.json ────────────────────────────────────────────────────
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=90)
        logs = AuditLog.query.filter(
            AuditLog.user_id == user.id,
            AuditLog.created_at >= since,
        ).order_by(AuditLog.created_at.desc()).all()

        logs_data: list[dict] = [
            {
                "action":      log.action,
                "entity_type": log.entity_type,
                "entity_id":   log.entity_id,
                "ip_address":  log.ip_address,
                "created_at":  log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]
        zf.writestr("audit_log.json", json.dumps(logs_data, indent=2))

        # ── README.txt ────────────────────────────────────────────────────────
        readme: str = (
            f"JobTracker Pro — Data Export\n"
            f"User: {user.name} ({user.email})\n"
            f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Files:\n"
            f"  profile.json       — your account info and settings\n"
            f"  applications.json  — all job applications with timeline\n"
            f"  resumes.json       — all resume versions with parsed data\n"
            f"  notifications.json — your notification history\n"
            f"  audit_log.json     — your activity log (last 90 days)\n"
        )
        zf.writestr("README.txt", readme)

    buf.seek(0)
    return buf


def _serialize_user(user: User) -> dict:
    """Safe user dict — no sensitive fields."""
    return {
        "id":            user.id,
        "name":          user.name,
        "email":         user.email,
        "avatar_url":    user.avatar_url,
        "is_verified":   user.is_verified,
        "totp_enabled":  user.totp_enabled,
        "has_linkedin":  bool(user.linkedin_id),
        "inbound_email": user.inbound_email,
        "created_at":    user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "settings":      _serialize_settings(user.settings) if user.settings else {},
    }


def _serialize_settings(s: UserSettings) -> dict:
    """Convert UserSettings to a plain dict."""
    if not s:
        return {}
    return {
        "theme":                  s.theme,
        "target_roles":           s.target_roles,
        "target_company_types":   s.target_company_types,
        "target_locations":       s.target_locations,
        "remote_preference":      s.remote_preference,
        "salary_min":             s.salary_min,
        "salary_max":             s.salary_max,
        "salary_currency":        s.salary_currency,
        "auto_apply_enabled":     s.auto_apply_enabled,
        "auto_apply_min_score":   s.auto_apply_min_score,
        "auto_apply_daily_limit": s.auto_apply_daily_limit,
        "auto_apply_portals":     s.auto_apply_portals,
        "auto_apply_hour":        s.auto_apply_hour,
        "notify_status_change":   s.notify_status_change,
        "notify_interview_remind":s.notify_interview_remind,
        "notify_weekly_digest":   s.notify_weekly_digest,
        "notify_bulk_done":       s.notify_bulk_done,
        "onboarding_complete":    s.onboarding_complete,
        "onboarding_step":        s.onboarding_step,
    }