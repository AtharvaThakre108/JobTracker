# app/utils/pagination.py
# ─────────────────────────────────────────────────────────────────────────────
# Reusable pagination for any SQLAlchemy query.
#
# Usage in a route:
#   query = JobApplication.query.filter_by(user_id=uid).order_by(...)
#   items, meta = paginate(query, page=1, per_page=20)
#   return success({"applications": [...], "pagination": meta})
#
# The frontend receives:
#   "pagination": {
#       "page": 1,
#       "per_page": 20,
#       "total": 143,
#       "pages": 8,
#       "has_next": true,
#       "has_prev": false
#   }
# ─────────────────────────────────────────────────────────────────────────────

from typing import Any
from flask import request


def paginate(
    query,
    page: int = 1,
    per_page: int = 20,
    max_per_page: int = 100,
) -> tuple[list[Any], dict]:
    """
    Paginate a SQLAlchemy query.

    Reads `page` and `per_page` from the request query string if not
    passed explicitly. Always respects max_per_page to prevent abuse
    (someone requesting per_page=99999).

    Args:
        query:        A SQLAlchemy Query object (NOT .all() — pass the query itself).
        page:         Page number to return (1-indexed).
        per_page:     Number of items per page.
        max_per_page: Hard ceiling on per_page regardless of what client sends.

    Returns:
        items: List of model instances for this page.
        meta:  Dict with pagination metadata for the API response.

    Example:
        items, meta = paginate(JobApplication.query.filter_by(user_id=uid))
    """

    # Allow client to override via query string: ?page=2&per_page=50
    try:
        page = int(request.args.get("page", page))
        per_page = int(request.args.get("per_page", per_page))
    except (ValueError, RuntimeError):
        pass  # Use defaults if outside request context or bad values

    # Clamp values to safe range
    page = max(1, page)                           # page can't be 0 or negative
    per_page = max(1, min(per_page, max_per_page)) # between 1 and max_per_page

    # SQLAlchemy's built-in paginator
    # error_out=False means page 999 of 3 returns empty list, not 404
    paginated = query.paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )

    meta: dict = {
        "page": paginated.page,
        "per_page": paginated.per_page,
        "total": paginated.total,        # Total rows matching the query
        "pages": paginated.pages,        # Total number of pages
        "has_next": paginated.has_next,
        "has_prev": paginated.has_prev,
    }

    return paginated.items, meta