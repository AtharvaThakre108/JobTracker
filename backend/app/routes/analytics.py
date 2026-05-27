# app/routes/analytics.py
# ─────────────────────────────────────────────────────────────────────────────
# Analytics endpoints — powers every chart and stat on the dashboard.
#
# ENDPOINTS:
#   GET /api/analytics/summary    — KPI cards (totals, rates, averages)
#   GET /api/analytics/trend      — applications over time (line chart data)
#   GET /api/analytics/funnel     — stage-by-stage conversion funnel
#   GET /api/analytics/heatmap    — 52-week daily activity grid
#   GET /api/analytics/breakdown  — company type + source distribution
#
# PERFORMANCE:
#   All queries use db.func aggregations — one DB round trip per endpoint.
#   No Python loops over full datasets.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional

from flask import Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, case

from app.extensions import db
from app.models import JobApplication, StatusHistory, AppStatus, CompanyType

analytics_bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/analytics/summary
# ─────────────────────────────────────────────────────────────────────────────

@analytics_bp.route("/summary", methods=["GET"])
@jwt_required()
def summary():
    """
    Return KPI summary cards for the dashboard header.

    Response includes:
        total           — all-time application count
        this_week       — applications in the last 7 days
        this_month      — applications in the last 30 days
        by_status       — count per status {Applied: 10, Interview: 3, ...}
        response_rate   — % of applications that got any response
        interview_rate  — % of responses that led to interview
        offer_rate      — % of interviews that led to offer
        ghosting_rate   — % of applications with no response
        avg_days_to_response — average days from apply to first status change
    """
    user_id: str = get_jwt_identity()
    now: datetime = datetime.now(timezone.utc)

    # ── Total counts ──────────────────────────────────────────────────────────
    total: int = JobApplication.query.filter_by(user_id=user_id).count()

    this_week: int = JobApplication.query.filter(
        JobApplication.user_id == user_id,
        JobApplication.applied_date >= now - timedelta(days=7),
    ).count()

    this_month: int = JobApplication.query.filter(
        JobApplication.user_id == user_id,
        JobApplication.applied_date >= now - timedelta(days=30),
    ).count()

    # ── Count per status ──────────────────────────────────────────────────────
    # One query, grouped — much faster than one query per status
    status_rows = db.session.query(
        JobApplication.status,
        func.count(JobApplication.id).label("count"),
    ).filter(
        JobApplication.user_id == user_id,
    ).group_by(
        JobApplication.status,
    ).all()

    by_status: dict = {s: 0 for s in AppStatus.ALL}
    for row in status_rows:
        by_status[row.status] = row.count

    # ── Conversion rates ──────────────────────────────────────────────────────
    responded: int = total - by_status.get(AppStatus.APPLIED, 0) - by_status.get(AppStatus.GHOSTED, 0)
    interviewed: int = by_status.get(AppStatus.INTERVIEW, 0) + by_status.get(AppStatus.OFFERED, 0)
    offered: int = by_status.get(AppStatus.OFFERED, 0)
    ghosted: int = by_status.get(AppStatus.GHOSTED, 0)

    def pct(numerator: int, denominator: int) -> float:
        """Safe percentage — returns 0.0 if denominator is zero."""
        return round(numerator / denominator * 100, 1) if denominator else 0.0

    response_rate:  float = pct(responded,   total)
    interview_rate: float = pct(interviewed, responded)
    offer_rate:     float = pct(offered,     interviewed)
    ghosting_rate:  float = pct(ghosted,     total)

    # ── Average days to first response ───────────────────────────────────────
    # Join applications to their first non-Applied status history entry
    first_change = db.session.query(
        StatusHistory.application_id,
        func.min(StatusHistory.changed_at).label("first_change_at"),
    ).filter(
        StatusHistory.new_status != AppStatus.APPLIED,
    ).group_by(
        StatusHistory.application_id,
    ).subquery()

    avg_days_row = db.session.query(
        func.avg(
            func.extract(
                "epoch",
                first_change.c.first_change_at - JobApplication.applied_date,
            ) / 86400   # Convert seconds → days
        )
    ).join(
        JobApplication,
        JobApplication.id == first_change.c.application_id,
    ).filter(
        JobApplication.user_id == user_id,
    ).scalar()

    avg_days: Optional[float] = round(float(avg_days_row), 1) if avg_days_row else None

    return _ok({
        "total":               total,
        "this_week":           this_week,
        "this_month":          this_month,
        "by_status":           by_status,
        "response_rate":       response_rate,
        "interview_rate":      interview_rate,
        "offer_rate":          offer_rate,
        "ghosting_rate":       ghosting_rate,
        "avg_days_to_response": avg_days,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/analytics/trend
# ─────────────────────────────────────────────────────────────────────────────

@analytics_bp.route("/trend", methods=["GET"])
@jwt_required()
def trend():
    """
    Return daily application counts for a line chart.

    Query params:
        days — number of days to look back (default: 30, max: 365)

    Response:
        [{"date": "2026-05-01", "count": 3}, ...]
        One entry per day in the range, 0 for days with no applications.
    """
    user_id: str = get_jwt_identity()

    # Clamp days between 7 and 365
    try:
        days: int = min(int(request.args.get("days", 30)), 365)
        days = max(days, 7)
    except ValueError:
        days = 30

    since: datetime = datetime.now(timezone.utc) - timedelta(days=days)

    # ── Query: count per day ──────────────────────────────────────────────────
    # func.date() truncates the timestamp to just the date part
    rows = db.session.query(
        func.date(JobApplication.applied_date).label("day"),
        func.count(JobApplication.id).label("count"),
    ).filter(
        JobApplication.user_id == user_id,
        JobApplication.applied_date >= since,
    ).group_by(
        func.date(JobApplication.applied_date),
    ).order_by(
        func.date(JobApplication.applied_date),
    ).all()

    # Build a lookup of date → count from DB results
    counts_by_date: dict = {str(row.day): row.count for row in rows}

    # Fill in every day in the range (including zeros) so chart has no gaps
    result: list[dict] = []
    for i in range(days):
        day = (since + timedelta(days=i)).date()
        result.append({
            "date":  str(day),
            "count": counts_by_date.get(str(day), 0),
        })

    return _ok({"trend": result, "days": days})


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/analytics/funnel
# ─────────────────────────────────────────────────────────────────────────────

@analytics_bp.route("/funnel", methods=["GET"])
@jwt_required()
def funnel():
    """
    Return funnel data — how many applications reached each stage.

    The funnel is cumulative downward:
        Applied → Responded → Interviewed → Offered → Accepted

    Response:
        [
          {"stage": "Applied",     "count": 142, "rate": 100.0},
          {"stage": "Responded",   "count": 48,  "rate": 33.8},
          {"stage": "Interviewed", "count": 21,  "rate": 14.8},
          {"stage": "Offered",     "count": 6,   "rate": 4.2},
        ]
    """
    user_id: str = get_jwt_identity()

    # Count per status in one query
    rows = db.session.query(
        JobApplication.status,
        func.count(JobApplication.id).label("count"),
    ).filter(
        JobApplication.user_id == user_id,
    ).group_by(
        JobApplication.status,
    ).all()

    counts: dict = {row.status: row.count for row in rows}
    total: int   = sum(counts.values())

    if not total:
        return _ok({"funnel": [], "total": 0})

    def safe_pct(n: int) -> float:
        return round(n / total * 100, 1) if total else 0.0

    applied:     int = total
    responded:   int = total - counts.get(AppStatus.APPLIED, 0) - counts.get(AppStatus.GHOSTED, 0)
    interviewed: int = counts.get(AppStatus.INTERVIEW, 0) + counts.get(AppStatus.OFFERED, 0)
    offered:     int = counts.get(AppStatus.OFFERED, 0)

    funnel_data: list[dict] = [
        {"stage": "Applied",     "count": applied,     "rate": 100.0},
        {"stage": "Responded",   "count": responded,   "rate": safe_pct(responded)},
        {"stage": "Interviewed", "count": interviewed, "rate": safe_pct(interviewed)},
        {"stage": "Offered",     "count": offered,     "rate": safe_pct(offered)},
    ]

    return _ok({"funnel": funnel_data, "total": total})


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/analytics/heatmap
# ─────────────────────────────────────────────────────────────────────────────

@analytics_bp.route("/heatmap", methods=["GET"])
@jwt_required()
def heatmap():
    """
    Return 52 weeks of daily activity for the GitHub-style heatmap.

    Response:
        {
          "weeks": [
            [{"date": "2025-06-01", "count": 0}, ...],  ← 7 days
            ...                                           ← 52 weeks
          ],
          "max_count": 5,     ← used by frontend to scale colour intensity
          "total":     142,
          "streak":    7,      ← current consecutive-day streak
        }
    """
    user_id: str = get_jwt_identity()
    today: datetime = datetime.now(timezone.utc).date()

    # Always start from 52 weeks ago on a Sunday (consistent grid)
    days_since_sunday: int = today.weekday() + 1  # Mon=0 → Sun offset
    grid_start = today - timedelta(days=(364 + days_since_sunday))

    # ── Query counts for the full year ────────────────────────────────────────
    rows = db.session.query(
        func.date(JobApplication.applied_date).label("day"),
        func.count(JobApplication.id).label("count"),
    ).filter(
        JobApplication.user_id == user_id,
        JobApplication.applied_date >= grid_start,
    ).group_by(
        func.date(JobApplication.applied_date),
    ).all()

    counts_by_date: dict = {str(row.day): row.count for row in rows}
    max_count: int = max(counts_by_date.values(), default=0)
    total: int     = sum(counts_by_date.values())

    # ── Build 52×7 grid ───────────────────────────────────────────────────────
    weeks: list[list[dict]] = []
    current_week: list[dict] = []

    for i in range(365):
        day = grid_start + timedelta(days=i)
        day_str: str = str(day)

        current_week.append({
            "date":  day_str,
            "count": counts_by_date.get(day_str, 0),
        })

        # New week every 7 days
        if len(current_week) == 7:
            weeks.append(current_week)
            current_week = []

    if current_week:
        weeks.append(current_week)

    # ── Calculate current streak ──────────────────────────────────────────────
    streak: int = 0
    check_day = today
    while True:
        if counts_by_date.get(str(check_day), 0) > 0:
            streak += 1
            check_day -= timedelta(days=1)
        else:
            break

    return _ok({
        "weeks":     weeks,
        "max_count": max_count,
        "total":     total,
        "streak":    streak,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/analytics/breakdown
# ─────────────────────────────────────────────────────────────────────────────

@analytics_bp.route("/breakdown", methods=["GET"])
@jwt_required()
def breakdown():
    """
    Return distribution breakdowns for pie charts.

    Response:
        {
          "by_company_type": [
            {"label": "Startup", "count": 45, "pct": 52.3},
            {"label": "MNC",     "count": 30, "pct": 34.9},
            {"label": "Business","count": 11, "pct": 12.8},
          ],
          "by_source": [
            {"label": "LinkedIn", "count": 60, "pct": 69.8},
            ...
          ],
          "by_location": [
            {"label": "Bangalore", "count": 40, "pct": 46.5},
            ...
          ]
        }
    """
    user_id: str = get_jwt_identity()

    def get_breakdown(column) -> list[dict]:
        """
        Generic breakdown query for any column.
        Returns sorted list of {label, count, pct} dicts.
        """
        rows = db.session.query(
            column.label("label"),
            func.count(JobApplication.id).label("count"),
        ).filter(
            JobApplication.user_id == user_id,
            column.isnot(None),
        ).group_by(
            column,
        ).order_by(
            func.count(JobApplication.id).desc(),
        ).all()

        total: int = sum(r.count for r in rows)

        return [
            {
                "label": row.label or "Unknown",
                "count": row.count,
                "pct":   round(row.count / total * 100, 1) if total else 0.0,
            }
            for row in rows
        ]

    return _ok({
        "by_company_type": get_breakdown(JobApplication.company_type),
        "by_source":       get_breakdown(JobApplication.source),
        "by_location":     get_breakdown(JobApplication.location),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Private helper
# ─────────────────────────────────────────────────────────────────────────────

def _ok(data: dict):
    """Thin wrapper — keeps route code clean."""
    from app.utils import success
    return success(data=data)