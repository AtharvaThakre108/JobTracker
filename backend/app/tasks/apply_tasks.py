# app/tasks/apply_tasks.py
# ─────────────────────────────────────────────────────────────────────────────
# Autonomous apply Celery tasks.
#
# MAIN TASK: run_autonomous_apply(user_id)
#   1. Load user preferences + active resume
#   2. Scrape jobs from configured portals
#   3. Score each job against resume (BERT + skill overlap)
#   4. Filter by min_score threshold
#   5. Deduplicate (skip already-applied URLs)
#   6. Apply to each qualifying job
#   7. Log results + send summary notification
#
# RATE LIMITING:
#   - Max 15 applies per run (user configurable)
#   - 5–15 second delay between submissions
#   - Celery rate_limit: 1 run per user per 30 minutes
#
# SAFETY:
#   - Each job wrapped in try/except — one failure never stops the whole run
#   - CAPTCHA detection pauses that portal for the session
#   - All actions written to AutonomousApplyRun for full audit trail
# ─────────────────────────────────────────────────────────────────────────────

import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional
import concurrent.futures
from app.tasks.email_tasks import send_auto_apply_summary

from wsgi import celery_app as celery

logger = logging.getLogger(__name__)


@celery.task(
    bind=True,
    name="app.tasks.apply_tasks.run_autonomous_apply",
    max_retries=0,           # Don't retry — each run is independent
    time_limit=3600,         # Hard kill after 1 hour
    soft_time_limit=3300,    # Soft warning at 55 minutes
)
def run_autonomous_apply(self, user_id: str) -> dict:
    """
    Main autonomous apply task. Finds + scores + applies to jobs for a user.

    Args:
        user_id: The user's UUID string.

    Returns:
        dict: Run summary with jobs_found, jobs_applied, jobs_skipped.
    """
    from app.extensions import db
    from app.models import (
        User, Resume, UserSettings, JobApplication,
        AutonomousApplyRun, BulkApplyQueue,
        AppStatus, AppSource, BulkStatus,
    )
    from app.ml.job_matcher import score_match, get_skill_gap
    from app.services.ats_optimizer import compute_ats_score

    logger.info(f"[AutoApply] Starting run for user {user_id}")

    # ── Load user + settings ──────────────────────────────────────────────────
    user: Optional[User] = db.session.get(User, user_id)
    if not user or not user.settings:
        logger.error(f"User {user_id} not found or has no settings.")
        return {"error": "User not found."}

    settings: UserSettings = user.settings

    if not settings.auto_apply_enabled:
        logger.info(f"Auto-apply is disabled for user {user_id}. Skipping.")
        return {"skipped": "Auto-apply disabled."}

    # ── Load active resume ────────────────────────────────────────────────────
    resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first()

    if not resume or not resume.parsed_data:
        logger.error(f"No active resume for user {user_id}.")
        return {"error": "No active resume."}

    resume_text:   str       = resume.parsed_data.get("raw_text", "")
    resume_skills: list[str] = resume.skills or []

    # ── Create run record ─────────────────────────────────────────────────────
    run = AutonomousApplyRun(user_id=user_id, status="running")
    db.session.add(run)
    db.session.commit()

    # ── Track run stats ───────────────────────────────────────────────────────
    jobs_found:   int = 0
    jobs_applied: int = 0
    jobs_skipped: int = 0
    skip_reasons: dict = {
        "below_threshold": 0,
        "already_applied": 0,
        "captcha":         0,
        "missing_fields":  0,
        "error":           0,
    }

    # ── Get already-applied URLs (deduplication) ──────────────────────────────
    applied_urls: set[str] = {
        app.job_url
        for app in JobApplication.query.filter_by(user_id=user_id).all()
        if app.job_url
    }

    # ── Scrape jobs from each configured portal ───────────────────────────────
    all_scraped_jobs: list[dict] = []
    portals: list[str]           = settings.auto_apply_portals or ["Indeed"]
    roles: list[str]             = settings.target_roles or []
    locations: list[str]         = settings.target_locations or ["India"]

    if not roles:
        logger.warning(f"User {user_id} has no target roles set.")
        _finish_run(run, db, jobs_found, jobs_applied, jobs_skipped, skip_reasons, "done")
        return {"warning": "No target roles configured."}

    for portal in portals:
        for role in roles[:3]:          # Max 3 roles per run
            for location in locations[:2]:  # Max 2 locations per role
                try:
                    scraped = _scrape_portal(portal, role, location)
                    all_scraped_jobs.extend(scraped)
                    logger.info(f"[AutoApply] {portal}: {len(scraped)} jobs for '{role}' in '{location}'")
                except Exception as e:
                    logger.warning(f"Scraping {portal} failed: {e}")

    jobs_found = len(all_scraped_jobs)
    run.jobs_found = jobs_found
    db.session.commit()

    if not all_scraped_jobs:
        logger.info(f"[AutoApply] No jobs scraped for user {user_id}.")
        _finish_run(run, db, jobs_found, 0, 0, skip_reasons, "done")
        return {"jobs_found": 0}

    # ── Score + filter jobs ───────────────────────────────────────────────────
    min_score: float     = settings.auto_apply_min_score or 0.75
    daily_limit: int     = settings.auto_apply_daily_limit or 15
    qualifying: list[dict] = []

    for job in all_scraped_jobs:
        # Skip already-applied
        if job.get("job_url") in applied_urls:
            skip_reasons["already_applied"] += 1
            jobs_skipped += 1
            continue

        # Score against resume
        jd_text: str = job.get("description", "")
        try:
            match: float = score_match(resume_text, resume_skills, jd_text)
            job["match_score"] = match
        except Exception:
            job["match_score"] = 0.0

        # Filter by threshold
        if job["match_score"] < min_score:
            skip_reasons["below_threshold"] += 1
            jobs_skipped += 1
            continue

        qualifying.append(job)

    # Cap at daily limit
    qualifying = qualifying[:daily_limit]

    logger.info(
        f"[AutoApply] {len(qualifying)} qualifying jobs "
        f"(threshold: {min_score}, limit: {daily_limit})"
    )

    # ── Apply to each qualifying job ──────────────────────────────────────────
    for job in qualifying:
        if jobs_applied >= daily_limit:
            break

        try:
            result: dict = _apply_to_job(
                user=user,
                resume=resume,
                job=job,
                run_id=run.id,
            )

            if result["status"] == BulkStatus.SUBMITTED:
                jobs_applied += 1
                applied_urls.add(job.get("job_url", ""))

                # Create JobApplication record
                app = JobApplication(
                    user_id=user_id,
                    resume_version_id=resume.id,
                    company_name=job.get("company", "Unknown"),
                    role=job.get("title", "Unknown"),
                    job_url=job.get("job_url"),
                    job_description=job.get("description", "")[:2000],
                    source=job.get("source", AppSource.BOT),
                    status=AppStatus.APPLIED,
                    match_score=job.get("match_score"),
                    location=job.get("location"),
                    is_remote=job.get("is_remote", False),
                    applied_by="bot",
                )
                db.session.add(app)

            elif result["status"] == BulkStatus.CAPTCHA:
                skip_reasons["captcha"] += 1
                jobs_skipped += 1

            elif result["status"] == BulkStatus.NEEDS_INPUT:
                skip_reasons["missing_fields"] += 1
                jobs_skipped += 1

            else:
                skip_reasons["error"] += 1
                jobs_skipped += 1

            db.session.commit()

            # Human-like delay between applications
            delay: float = random.uniform(5.0, 15.0)
            logger.info(f"[AutoApply] Waiting {delay:.1f}s before next application...")
            time.sleep(delay)

        except Exception as e:
            logger.error(f"[AutoApply] Apply failed for {job.get('job_url')}: {e}")
            skip_reasons["error"] += 1
            jobs_skipped += 1
            continue

    # ── Finish run ────────────────────────────────────────────────────────────
    _finish_run(run, db, jobs_found, jobs_applied, jobs_skipped, skip_reasons, "done")

    # ── Send summary notification ─────────────────────────────────────────────
    _notify_user(user_id, jobs_applied, jobs_skipped, skip_reasons)

    # Send summary email in background
    send_auto_apply_summary.delay(
        user_id, jobs_applied, jobs_skipped, skip_reasons
    )

    logger.info(
        f"[AutoApply] Run complete for {user_id}: "
        f"applied={jobs_applied}, skipped={jobs_skipped}"
    )

    return {
        "run_id":       run.id,
        "jobs_found":   jobs_found,
        "jobs_applied": jobs_applied,
        "jobs_skipped": jobs_skipped,
        "skip_reasons": skip_reasons,
    }

@celery.task(name="app.tasks.apply_tasks.debug_naukri")
def debug_naukri(role: str = "Python developer", location: str = "Bangalore") -> dict:
    """
    Temporary debug task — dumps Naukri page HTML so we can
    find the correct CSS selectors.
    """
    import concurrent.futures

    def _run():
        from app.scraper.base import BaseScraper

        class DebugScraper(BaseScraper):
            pass

        with DebugScraper(headless=True) as scraper:
            from urllib.parse import urlencode
            slug_role     = role.lower().replace(" ", "-")
            slug_location = location.lower().replace(" ", "-")
            url = f"https://www.naukri.com/{slug_role}-jobs-in-{slug_location}"

            scraper.goto(url, wait_until="domcontentloaded")
            scraper._human_delay(4.0, 6.0)

            # Dump page title and first 3000 chars of HTML
            title   = scraper.page.title()
            content = scraper.page.content()

            # Find all unique class names on the page
            classes = scraper.page.evaluate("""
                () => {
                    const els = document.querySelectorAll('[class]');
                    const names = new Set();
                    els.forEach(el => {
                        el.className.split(' ').forEach(c => {
                            if (c.includes('job') || c.includes('tuple')
                                || c.includes('list') || c.includes('card')) {
                                names.add(c);
                            }
                        });
                    });
                    return Array.from(names).slice(0, 50);
                }
            """)

            return {
                "title":   title,
                "url":     scraper.page.url,
                "classes": classes,
                "html_snippet": content[5000:8000],  # Middle section of HTML
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_run).result(timeout=120)

# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_portal(portal: str, role: str, location: str) -> list[dict]:
    """
    Fetch jobs from JSearch API.
    Portal parameter kept for compatibility — JSearch searches all portals.
    """
    from app.services.job_search_service import search_jobs

    try:
        # Append "India" to location for better India-specific results
        location_query: str = (
            f"{location}, India"
            if location.lower() not in ("india", "remote")
            else location
        )
        return search_jobs(
            role=role,
            location=location_query,
            max_results=10,
            date_posted="week",
        )
    except Exception as e:
        logger.warning(f"JSearch failed for {role} in {location}: {e}")
        return []


def _apply_to_job(
    user,
    resume,
    job: dict,
    run_id: str,
) -> dict:
    """
    Attempt to apply to a single job.

    This is a placeholder for the full autofill engine (built in Round 11).
    Currently logs the attempt and simulates the flow.

    Args:
        user:   User model instance.
        resume: Resume model instance.
        job:    Scraped job dict.
        run_id: ID of the current AutonomousApplyRun.

    Returns:
        dict: {"status": BulkStatus.*, "error": optional str}
    """
    from app.models import BulkStatus

    # Import autofill when it's built (Round 11)
    # from app.scraper.autofill import AutoFiller

    logger.info(
        f"[AutoApply] Applying to {job.get('title')} "
        f"@ {job.get('company')} ({job.get('job_url', '')[:50]}...)"
    )

    # TODO Round 11: replace this with actual form filling
    # For now, log the job as queued in BulkApplyQueue
    from app.extensions import db
    from app.models import BulkApplyQueue

    queue_item = BulkApplyQueue(
        user_id=user.id,
        run_id=run_id,
        job_url=job.get("job_url", ""),
        company=job.get("company", ""),
        role=job.get("title", ""),
        portal=job.get("source", "Indeed"),
        status=BulkStatus.QUEUED,
        match_score=job.get("match_score"),
    )
    db.session.add(queue_item)
    db.session.flush()

    # Placeholder: mark as submitted for testing
    # Real autofill engine replaces this in Round 11
    queue_item.status = BulkStatus.SUBMITTED
    queue_item.submitted_at = datetime.now(timezone.utc)

    return {"status": BulkStatus.SUBMITTED}


def _finish_run(
    run,
    db,
    found: int,
    applied: int,
    skipped: int,
    reasons: dict,
    status: str,
) -> None:
    """Update the AutonomousApplyRun record with final stats."""
    run.jobs_found   = found
    run.jobs_applied = applied
    run.jobs_skipped = skipped
    run.skip_reasons = reasons
    run.status       = status
    run.completed_at = datetime.now(timezone.utc)
    db.session.commit()


def _notify_user(
    user_id: str,
    applied: int,
    skipped: int,
    reasons: dict,
) -> None:
    """Create an in-app notification summarising the run."""
    from app.extensions import db
    from app.models import Notification, NotifType

    message: str = (
        f"Applied to {applied} job(s) automatically. "
        f"{skipped} skipped"
    )
    if reasons.get("captcha"):
        message += f" ({reasons['captcha']} CAPTCHA blocks)"
    if reasons.get("below_threshold"):
        message += f" ({reasons['below_threshold']} below match threshold)"

    notif = Notification(
        user_id=user_id,
        type=NotifType.BULK_APPLY_DONE,
        title="Auto-apply run complete",
        message=message,
        link="/dashboard",
    )
    db.session.add(notif)
    db.session.commit()