# app/services/email_service.py
# ─────────────────────────────────────────────────────────────────────────────
# Transactional email via SendGrid.
#
# WHY SendGrid:
#   - Free tier: 100 emails/day forever
#   - Reliable delivery + spam compliance
#   - Simple REST API — no SMTP config needed
#   - Delivery tracking (opens, clicks) in dashboard
#
# ALL emails in the system go through send_email().
# Add new email types by adding new template functions below.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional

from flask import current_app

logger = logging.getLogger(__name__)


def send_email(
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    plain_body: Optional[str] = None,
) -> bool:
    """
    Send a transactional email via SendGrid.

    Args:
        to_email:   Recipient email address.
        to_name:    Recipient display name.
        subject:    Email subject line.
        html_body:  HTML content of the email.
        plain_body: Plain text fallback (auto-generated if not provided).

    Returns:
        bool: True if sent successfully, False otherwise.
    """
    api_key: str = current_app.config.get("SENDGRID_API_KEY", "")

    if not api_key:
        # Log the email content in dev so we can verify it without SendGrid
        logger.warning(
            f"SendGrid API key not set. Email NOT sent.\n"
            f"TO: {to_email}\nSUBJECT: {subject}\n"
            f"BODY: {plain_body or 'See HTML body'}"
        )
        return False

    from_email: str = current_app.config.get("FROM_EMAIL", "noreply@jobtracker.app")
    from_name:  str = current_app.config.get("FROM_NAME",  "JobTracker Pro")

    # Auto-generate plain text from HTML if not provided
    if not plain_body:
        import re
        plain_body = re.sub(r"<[^>]+>", "", html_body)
        plain_body = re.sub(r"\s+", " ", plain_body).strip()

    payload: dict = {
        "personalizations": [{
            "to": [{"email": to_email, "name": to_name}],
        }],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html",  "value": html_body},
        ],
    }

    try:
        import requests
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )

        if response.status_code == 202:
            logger.info(f"Email sent to {to_email}: {subject}")
            return True
        else:
            logger.error(
                f"SendGrid error {response.status_code}: {response.text[:200]}"
            )
            return False

    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Email templates
#  Each function returns (subject, html_body) ready for send_email()
# ─────────────────────────────────────────────────────────────────────────────

def email_verification_template(name: str, verify_url: str) -> tuple[str, str]:
    """Email verification link sent on registration."""
    subject = "Verify your JobTracker Pro account"
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#6366f1;">JobTracker Pro</h2>
        <p>Hi {name},</p>
        <p>Thanks for signing up. Click the button below to verify your email address.</p>
        <a href="{verify_url}"
           style="display:inline-block;background:#6366f1;color:white;
                  padding:12px 24px;border-radius:6px;text-decoration:none;
                  font-weight:bold;margin:16px 0;">
            Verify Email
        </a>
        <p style="color:#64748b;font-size:14px;">
            Link expires in 24 hours. If you didn't create an account, ignore this email.
        </p>
    </div>
    """
    return subject, html


def status_change_template(
    name: str,
    company: str,
    role: str,
    old_status: str,
    new_status: str,
    app_url: str,
) -> tuple[str, str]:
    """Sent when an application status changes."""

    # Colour per status
    status_colors: dict = {
        "Interview": "#f0a500",
        "Offered":   "#27ae60",
        "Rejected":  "#e74c3c",
        "Ghosted":   "#95a5a6",
        "Applied":   "#4f86c6",
    }
    color: str = status_colors.get(new_status, "#6366f1")

    subject = f"Application update: {company} → {new_status}"
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#6366f1;">JobTracker Pro</h2>
        <p>Hi {name},</p>
        <p>Your application status has been updated:</p>
        <div style="background:#f8fafc;border-left:4px solid {color};
                    padding:16px;border-radius:4px;margin:16px 0;">
            <strong>{role}</strong> at <strong>{company}</strong><br>
            <span style="color:#64748b;">{old_status}</span>
            &nbsp;→&nbsp;
            <span style="color:{color};font-weight:bold;">{new_status}</span>
        </div>
        <a href="{app_url}"
           style="display:inline-block;background:#6366f1;color:white;
                  padding:12px 24px;border-radius:6px;text-decoration:none;
                  font-weight:bold;">
            View Application
        </a>
    </div>
    """
    return subject, html


def interview_reminder_template(
    name: str,
    company: str,
    role: str,
    interview_date: str,
    app_url: str,
) -> tuple[str, str]:
    """Reminder sent 24h before an interview."""
    subject = f"Interview reminder: {company} tomorrow"
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#6366f1;">JobTracker Pro</h2>
        <p>Hi {name},</p>
        <p>You have an interview scheduled for tomorrow — good luck! 🎯</p>
        <div style="background:#f0fdf4;border-left:4px solid #27ae60;
                    padding:16px;border-radius:4px;margin:16px 0;">
            <strong>{role}</strong> at <strong>{company}</strong><br>
            <span style="color:#64748b;">📅 {interview_date}</span>
        </div>
        <a href="{app_url}"
           style="display:inline-block;background:#6366f1;color:white;
                  padding:12px 24px;border-radius:6px;text-decoration:none;
                  font-weight:bold;">
            View Interview Prep
        </a>
        <p style="color:#64748b;font-size:14px;">
            Review your prep questions and notes before the interview.
        </p>
    </div>
    """
    return subject, html


def weekly_digest_template(
    name: str,
    stats: dict,
    dashboard_url: str,
) -> tuple[str, str]:
    """
    Weekly summary email sent every Monday morning.

    Args:
        stats: Dict from analytics/summary endpoint.
    """
    subject = "Your weekly job search summary"
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#6366f1;">JobTracker Pro — Weekly Digest</h2>
        <p>Hi {name}, here's your job search summary for the week:</p>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0;">
            <div style="background:#f8fafc;padding:16px;border-radius:8px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#6366f1;">
                    {stats.get("this_week", 0)}
                </div>
                <div style="color:#64748b;font-size:14px;">Applied this week</div>
            </div>
            <div style="background:#f8fafc;padding:16px;border-radius:8px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#f0a500;">
                    {stats.get("by_status", {}).get("Interview", 0)}
                </div>
                <div style="color:#64748b;font-size:14px;">Interviews</div>
            </div>
            <div style="background:#f8fafc;padding:16px;border-radius:8px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#27ae60;">
                    {stats.get("response_rate", 0)}%
                </div>
                <div style="color:#64748b;font-size:14px;">Response rate</div>
            </div>
            <div style="background:#f8fafc;padding:16px;border-radius:8px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#e74c3c;">
                    {stats.get("by_status", {}).get("Rejected", 0)}
                </div>
                <div style="color:#64748b;font-size:14px;">Rejections</div>
            </div>
        </div>

        <a href="{dashboard_url}"
           style="display:inline-block;background:#6366f1;color:white;
                  padding:12px 24px;border-radius:6px;text-decoration:none;
                  font-weight:bold;margin-top:8px;">
            View Dashboard
        </a>
    </div>
    """
    return subject, html


def auto_apply_summary_template(
    name: str,
    jobs_applied: int,
    jobs_skipped: int,
    skip_reasons: dict,
    dashboard_url: str,
) -> tuple[str, str]:
    """Summary email after an autonomous apply run completes."""
    subject = f"Auto-apply complete: {jobs_applied} applications submitted"

    reasons_html: str = ""
    if skip_reasons:
        reasons_html = "<ul style='color:#64748b;font-size:14px;'>"
        if skip_reasons.get("below_threshold"):
            reasons_html += f"<li>{skip_reasons['below_threshold']} below match threshold</li>"
        if skip_reasons.get("captcha"):
            reasons_html += f"<li>{skip_reasons['captcha']} blocked by CAPTCHA</li>"
        if skip_reasons.get("already_applied"):
            reasons_html += f"<li>{skip_reasons['already_applied']} already applied</li>"
        reasons_html += "</ul>"

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#6366f1;">JobTracker Pro — Auto-Apply Summary</h2>
        <p>Hi {name}, your auto-apply run has completed.</p>

        <div style="background:#f0fdf4;border-left:4px solid #27ae60;
                    padding:16px;border-radius:4px;margin:16px 0;">
            <strong style="font-size:24px;color:#27ae60;">✅ {jobs_applied}</strong>
            <span style="color:#64748b;"> applications submitted</span>
        </div>

        {"<p><strong>" + str(jobs_skipped) + " skipped:</strong></p>" + reasons_html if jobs_skipped else ""}

        <a href="{dashboard_url}"
           style="display:inline-block;background:#6366f1;color:white;
                  padding:12px 24px;border-radius:6px;text-decoration:none;
                  font-weight:bold;">
            View All Applications
        </a>
    </div>
    """
    return subject, html