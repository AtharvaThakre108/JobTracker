# app/utils/audit.py
# ─────────────────────────────────────────────────────────────────────────────
# Audit logging — records every important action a user takes.
#
# WHAT gets logged:
#   auth.register, auth.login, auth.login_failed, auth.logout
#   auth.2fa_enabled, auth.2fa_disabled, auth.2fa_backup_used
#   auth.linkedin_login, auth.email_verified
#   application.created, application.status_changed, application.deleted
#   resume.uploaded, resume.deleted, resume.activated
#   bulk_apply.run_started, bulk_apply.run_completed
#   user.settings_changed, user.data_exported, user.account_deleted
#
# WHY silent failure:
#   If the audit log write fails (e.g. DB blip), we log a warning but do NOT
#   raise an exception. The actual user action already succeeded — we should
#   not roll it back just because of a logging side-effect.
#
# Usage:
#   log_action("application.created", entity_type="application", entity_id=app.id)
#   log_action("application.status_changed",
#              entity_type="application", entity_id=app.id,
#              old_value={"status": "Applied"},
#              new_value={"status": "Interview"})
# ─────────────────────────────────────────────────────────────────────────────

from typing import Optional
from flask import request, current_app
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request


def log_action(
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    old_value: Optional[dict] = None,
    new_value: Optional[dict] = None,
    user_id: Optional[str] = None,
) -> None:
    """
    Write one row to the audit_log table.

    Args:
        action:      Dot-namespaced event name. e.g. "auth.login"
        entity_type: The kind of thing being acted on. e.g. "application"
        entity_id:   The UUID of that thing.
        old_value:   Dict of values BEFORE the change (for update events).
        new_value:   Dict of values AFTER the change (for create/update events).
        user_id:     Explicit user ID. If omitted, read from the current JWT.

    Returns:
        None — always. Never raises. Logs a warning on failure.
    """

    # Lazy imports — avoids circular import since models import from utils
    from app.extensions import db
    from app.models import AuditLog

    try:
        # Resolve user_id from JWT if not passed explicitly
        uid: Optional[str] = user_id
        if not uid:
            try:
                # optional=True means this won't raise if there's no token
                verify_jwt_in_request(optional=True)
                uid = get_jwt_identity()
            except Exception:
                pass  # No token in context — uid stays None (e.g. failed login)

        # Safely grab request context fields
        # These can be None if called outside a request context (e.g. Celery task)
        ip: Optional[str] = None
        ua: Optional[str] = None

        try:
            ip = request.remote_addr
            ua = (request.headers.get("User-Agent") or "")[:500]
        except RuntimeError:
            pass  # Outside request context — fine, just omit ip/ua

        entry = AuditLog(
            user_id=uid,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            ip_address=ip,
            user_agent=ua,
        )

        db.session.add(entry)
        db.session.commit()

    except Exception as exc:
        # Never crash the caller — just warn
        try:
            current_app.logger.warning(f"[audit] Failed to log action '{action}': {exc}")
        except RuntimeError:
            pass  # Outside app context entirely — nothing we can do