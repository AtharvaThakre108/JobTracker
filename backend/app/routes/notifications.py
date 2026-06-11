# app/routes/notifications.py
# ─────────────────────────────────────────────────────────────────────────────
# In-app notification inbox endpoints.
#
# ENDPOINTS:
#   GET  /api/notifications           — list notifications (paginated)
#   PUT  /api/notifications/<id>/read — mark one as read
#   PUT  /api/notifications/read-all  — mark all as read
#   DELETE /api/notifications/<id>    — delete one notification
#   GET  /api/notifications/unread-count — badge count for navbar
# ─────────────────────────────────────────────────────────────────────────────

from typing import Optional

from flask import Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models import Notification
from app.utils import success, error, paginate

notifications_bp = Blueprint(
    "notifications", __name__, url_prefix="/api/notifications"
)


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/notifications
# ─────────────────────────────────────────────────────────────────────────────

@notifications_bp.route("", methods=["GET"])
@jwt_required()
def list_notifications():
    """
    Return paginated notifications for the current user, newest first.

    Query params:
        unread_only — "true" to return only unread (default: false)
        page        — page number (default: 1)
        per_page    — results per page (default: 20)
    """
    user_id: str = get_jwt_identity()

    query = Notification.query.filter_by(
        user_id=user_id
    ).order_by(Notification.created_at.desc())

    # Optional filter — unread only
    if request.args.get("unread_only", "").lower() == "true":
        query = query.filter_by(is_read=False)

    items, meta = paginate(query)

    return success(data={
        "notifications": [_serialize(n) for n in items],
        "pagination":    meta,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/notifications/unread-count
# ─────────────────────────────────────────────────────────────────────────────

@notifications_bp.route("/unread-count", methods=["GET"])
@jwt_required()
def unread_count():
    """
    Return the count of unread notifications.
    Used to show the badge number on the notification bell in the navbar.
    Lightweight — no pagination needed.
    """
    user_id: str = get_jwt_identity()

    count: int = Notification.query.filter_by(
        user_id=user_id,
        is_read=False,
    ).count()

    return success(data={"unread_count": count})


# ─────────────────────────────────────────────────────────────────────────────
#  PUT /api/notifications/<id>/read
# ─────────────────────────────────────────────────────────────────────────────

@notifications_bp.route("/<string:notif_id>/read", methods=["PUT"])
@jwt_required()
def mark_read(notif_id: str):
    """
    Mark a single notification as read.
    Called when the user clicks a notification in the dropdown.
    """
    user_id: str = get_jwt_identity()

    notif: Optional[Notification] = Notification.query.filter_by(
        id=notif_id,
        user_id=user_id,
    ).first()

    if not notif:
        return error("Notification not found.", 404)

    notif.is_read = True
    db.session.commit()

    return success(
        data={"notification": _serialize(notif)},
        message="Marked as read.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  PUT /api/notifications/read-all
# ─────────────────────────────────────────────────────────────────────────────

@notifications_bp.route("/read-all", methods=["PUT"])
@jwt_required()
def mark_all_read():
    """
    Mark all unread notifications as read.
    Called when user clicks "Mark all as read".
    """
    user_id: str = get_jwt_identity()

    updated: int = Notification.query.filter_by(
        user_id=user_id,
        is_read=False,
    ).update({"is_read": True})

    db.session.commit()

    return success(
        data={"marked_read": updated},
        message=f"Marked {updated} notification(s) as read.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE /api/notifications/<id>
# ─────────────────────────────────────────────────────────────────────────────

@notifications_bp.route("/<string:notif_id>", methods=["DELETE"])
@jwt_required()
def delete_notification(notif_id: str):
    """Delete a single notification."""
    user_id: str = get_jwt_identity()

    notif: Optional[Notification] = Notification.query.filter_by(
        id=notif_id,
        user_id=user_id,
    ).first()

    if not notif:
        return error("Notification not found.", 404)

    db.session.delete(notif)
    db.session.commit()

    return success(message="Notification deleted.")


# ─────────────────────────────────────────────────────────────────────────────
#  Private helper
# ─────────────────────────────────────────────────────────────────────────────

def _serialize(n: Notification) -> dict:
    """Convert a Notification to a safe API dict."""
    return {
        "id":         n.id,
        "type":       n.type,
        "title":      n.title,
        "message":    n.message,
        "link":       n.link,
        "is_read":    n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }