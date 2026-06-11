# app/tasks/email_tasks.py
# ─────────────────────────────────────────────────────────────────────────────
# Background email tasks — all emails sent via Celery workers.
#
# WHY async:
#   Email delivery takes 200–500ms. Running it in a Celery task means
#   API routes respond instantly instead of waiting for SendGrid.
#
# TASKS:
#   send_verification_email    — on registration
#   send_status_change_email   — on application status update
#   send_interview_reminder    — 24h before interview
#   send_weekly_digest         — every Monday 9am
#   send_auto_apply_summary    — after each auto-apply run
# ─────────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional
from datetime import datetime, timezone

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.email_tasks.send_verification_email")
def send_verification_email(user_id: str) -> bool:
    """
    Send email verification link to a newly registered user.

    Args:
        user_id: The user's UUID.

    Returns:
        bool: True if sent successfully.
    """
    from app.extensions import db
    from app.models import User
    from app.services.email_service import (
        send_email, email_verification_template
    )
    from flask import current_app

    user: Optional[User] = db.session.get(User, user_id)
    if not user or not user.verification_token:
        logger.warning(f"Cannot send verification email — user {user_id} not found or already verified.")
        return False

    frontend_url: str = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
    verify_url:   str = f"{frontend_url}/auth/verify/{user.verification_token}"

    subject, html = email_verification_template(user.name, verify_url)
    return send_email(user.email, user.name, subject, html)


@shared_task(name="app.tasks.email_tasks.send_status_change_email")
def send_status_change_email(
    user_id: str,
    application_id: str,
    old_status: str,
    new_status: str,
) -> bool:
    """
    Notify user by email when an application status changes.

    Args:
        user_id:        The user's UUID.
        application_id: The application's UUID.
        old_status:     Previous status string.
        new_status:     New status string.
    """
    from app.extensions import db
    from app.models import User, JobApplication
    from app.services.email_service import (
        send_email, status_change_template
    )
    from flask import current_app

    user: Optional[User] = db.session.get(User, user_id)
    app  = db.session.get(JobApplication, application_id)

    if not user or not app:
        return False

    # Respect user notification preferences
    if user.settings and not user.settings.notify_status_change:
        return False

    frontend_url: str = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
    app_url:      str = f"{frontend_url}/applications/{application_id}"

    subject, html = status_change_template(
        name=user.name,
        company=app.company_name,
        role=app.role,
        old_status=old_status,
        new_status=new_status,
        app_url=app_url,
    )
    return send_email(user.email, user.name, subject, html)


@shared_task(name="app.tasks.email_tasks.send_interview_reminder")
def send_interview_reminder(user_id: str, application_id: str) -> bool:
    """
    Send a 24-hour interview reminder email.
    Scheduled by the calendar service when interview_date is set.

    Args:
        user_id:        The user's UUID.
        application_id: The application UUID with the interview.
    """
    from app.extensions import db
    from app.models import User, JobApplication
    from app.services.email_service import (
        send_email, interview_reminder_template
    )
    from flask import current_app

    user: Optional[User] = db.session.get(User, user_id)
    app  = db.session.get(JobApplication, application_id)

    if not user or not app:
        return False

    if user.settings and not user.settings.notify_interview_remind:
        return False

    if not app.interview_date:
        return False

    interview_str: str = app.interview_date.strftime("%A, %B %d at %I:%M %p IST")
    frontend_url:  str = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
    app_url:       str = f"{frontend_url}/applications/{application_id}"

    subject, html = interview_reminder_template(
        name=user.name,
        company=app.company_name,
        role=app.role,
        interview_date=interview_str,
        app_url=app_url,
    )
    return send_email(user.email, user.name, subject, html)


@shared_task(name="app.tasks.email_tasks.send_weekly_digest")
def send_weekly_digest(user_id: str) -> bool:
    """
    Send the weekly job search summary email.
    Triggered by Celery Beat every Monday at 9am IST.

    Args:
        user_id: The user's UUID.
    """
    from app.extensions import db
    from app.models import User, JobApplication, AppStatus
    from app.services.email_service import send_email, weekly_digest_template
    from flask import current_app
    from datetime import timedelta
    from sqlalchemy import func

    user: Optional[User] = db.session.get(User, user_id)
    if not user:
        return False

    if user.settings and not user.settings.notify_weekly_digest:
        return False

    # Build stats for the digest
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    this_week: int = JobApplication.query.filter(
        JobApplication.user_id == user_id,
        JobApplication.applied_date >= week_ago,
    ).count()

    # Count per status
    status_rows = db.session.query(
        JobApplication.status,
        func.count(JobApplication.id),
    ).filter(
        JobApplication.user_id == user_id,
    ).group_by(JobApplication.status).all()

    by_status: dict = {s: 0 for s in AppStatus.ALL}
    for status, count in status_rows:
        by_status[status] = count

    total: int = sum(by_status.values())
    responded: int = total - by_status.get("Applied", 0) - by_status.get("Ghosted", 0)
    response_rate: float = round(responded / total * 100, 1) if total else 0.0

    stats: dict = {
        "this_week":     this_week,
        "by_status":     by_status,
        "response_rate": response_rate,
    }

    frontend_url:   str = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
    dashboard_url:  str = f"{frontend_url}/dashboard"

    subject, html = weekly_digest_template(user.name, stats, dashboard_url)
    return send_email(user.email, user.name, subject, html)


@shared_task(name="app.tasks.email_tasks.send_auto_apply_summary")
def send_auto_apply_summary(
    user_id: str,
    jobs_applied: int,
    jobs_skipped: int,
    skip_reasons: dict,
) -> bool:
    """
    Email summary after an autonomous apply run completes.

    Args:
        user_id:      The user's UUID.
        jobs_applied: Number of jobs successfully applied to.
        jobs_skipped: Number of jobs skipped.
        skip_reasons: Dict of skip reason counts.
    """
    from app.extensions import db
    from app.models import User
    from app.services.email_service import (
        send_email, auto_apply_summary_template
    )
    from flask import current_app

    user: Optional[User] = db.session.get(User, user_id)
    if not user:
        return False

    if user.settings and not user.settings.notify_bulk_done:
        return False

    frontend_url:  str = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
    dashboard_url: str = f"{frontend_url}/applications"

    subject, html = auto_apply_summary_template(
        name=user.name,
        jobs_applied=jobs_applied,
        jobs_skipped=jobs_skipped,
        skip_reasons=skip_reasons,
        dashboard_url=dashboard_url,
    )
    return send_email(user.email, user.name, subject, html)