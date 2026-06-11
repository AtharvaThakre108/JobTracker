# app/services/job_search_service.py
# ─────────────────────────────────────────────────────────────────────────────
# Job search via JSearch API (RapidAPI).
#
# WHY API over scraping:
#   Scraping major portals (Indeed, Naukri, LinkedIn) is blocked by
#   bot detection in 2025/2026. JSearch provides a legitimate API that
#   searches all portals simultaneously — one call, no browser needed.
#
# API: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
# Free tier: 200 requests/month
# Paid: $10/month for 2000 requests
#
# RESPONSE FORMAT:
#   Returns same job dict structure as scrapers so apply_tasks.py
#   doesn't need any changes — drop-in replacement.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional

import requests
from flask import current_app

logger = logging.getLogger(__name__)

JSEARCH_BASE: str = "https://jsearch.p.rapidapi.com"


def search_jobs(
    role: str,
    location: str,
    max_results: int = 10,
    employment_type: str = "FULLTIME",
    date_posted: str = "week",
) -> list[dict]:
    """
    Search for jobs using JSearch API.

    Args:
        role:            Job title / keywords e.g. "Python Backend Developer"
        location:        City or country e.g. "Bangalore, India"
        max_results:     Max jobs to return (max 10 per API call on free tier)
        employment_type: "FULLTIME" | "PARTTIME" | "INTERN" | "CONTRACTOR"
        date_posted:     "today" | "3days" | "week" | "month"

    Returns:
        List of job dicts with keys:
            title, company, location, description,
            job_url, salary, is_remote, source, job_id
    """
    api_key: str = current_app.config.get("RAPIDAPI_KEY", "")

    if not api_key:
        logger.error(
            "RAPIDAPI_KEY not set in .env. "
            "Get a free key at https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch"
        )
        return []

    # ── Build query ───────────────────────────────────────────────────────────
    # JSearch accepts natural language queries
    query: str = f"{role} in {location}" if location else role

    params: dict = {
        "query":           query,
        "page":            "1",
        "num_pages":       "1",
        "employment_types": employment_type,
        "date_posted":     date_posted,
        "remote_jobs_only": "false",
    }

    headers: dict = {
        "X-RapidAPI-Key":  api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    logger.info(f"JSearch API: searching '{query}'")

    try:
        response = requests.get(
            f"{JSEARCH_BASE}/search",
            headers=headers,
            params=params,
            timeout=15,
        )

        if response.status_code == 429:
            logger.warning("JSearch API rate limit hit. Try again later.")
            return []

        if response.status_code == 403:
            logger.error("JSearch API key invalid or subscription expired.")
            return []

        if not response.ok:
            logger.error(
                f"JSearch API error: {response.status_code} — {response.text[:200]}"
            )
            return []

        data: dict = response.json()
        raw_jobs: list[dict] = data.get("data", [])

        if not raw_jobs:
            logger.info(f"JSearch: no results for '{query}'")
            return []

        # ── Normalise to our standard job dict format ─────────────────────────
        jobs: list[dict] = []
        for raw in raw_jobs[:max_results]:
            job: Optional[dict] = _normalise(raw)
            if job:
                jobs.append(job)

        logger.info(f"JSearch: found {len(jobs)} jobs for '{query}'")
        return jobs

    except requests.Timeout:
        logger.warning("JSearch API request timed out.")
        return []
    except Exception as e:
        logger.error(f"JSearch API call failed: {e}")
        return []


def get_job_details(job_id: str) -> Optional[dict]:
    """
    Fetch full details for a specific job by its JSearch job ID.

    Args:
        job_id: The job_id from a previous search result.

    Returns:
        Full job dict or None if not found.
    """
    api_key: str = current_app.config.get("RAPIDAPI_KEY", "")
    if not api_key:
        return None

    headers: dict = {
        "X-RapidAPI-Key":  api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    try:
        response = requests.get(
            f"{JSEARCH_BASE}/job-details",
            headers=headers,
            params={"job_id": job_id, "extended_publisher_details": "false"},
            timeout=15,
        )

        if not response.ok:
            return None

        data: dict        = response.json()
        raw_jobs: list    = data.get("data", [])
        return _normalise(raw_jobs[0]) if raw_jobs else None

    except Exception as e:
        logger.warning(f"JSearch job details failed for {job_id}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(raw: dict) -> Optional[dict]:
    """
    Convert a raw JSearch API job result to our standard job dict.

    JSearch returns deeply nested objects — we flatten what we need.

    Args:
        raw: Raw job dict from JSearch API response.

    Returns:
        Normalised job dict, or None if essential fields are missing.
    """
    # ── Required fields ───────────────────────────────────────────────────────
    title:   str = (raw.get("job_title")   or "").strip()
    company: str = (raw.get("employer_name") or "").strip()

    if not title or not company:
        return None

    # ── Location ──────────────────────────────────────────────────────────────
    city:    str = raw.get("job_city")    or ""
    country: str = raw.get("job_country") or ""
    state:   str = raw.get("job_state")   or ""
    location: str = ", ".join(filter(None, [city, state, country]))

    # ── Job URL ───────────────────────────────────────────────────────────────
    # Prefer direct apply URL, fall back to listing URL
    job_url: str = (
        raw.get("job_apply_link") or
        raw.get("job_google_link") or
        ""
    )

    # ── Description ───────────────────────────────────────────────────────────
    description: str = (raw.get("job_description") or "")[:3000]

    # ── Salary ────────────────────────────────────────────────────────────────
    salary_min: Optional[float] = raw.get("job_min_salary")
    salary_max: Optional[float] = raw.get("job_max_salary")
    salary_currency: str        = raw.get("job_salary_currency") or "INR"
    salary_period: str          = raw.get("job_salary_period") or ""

    if salary_min and salary_max:
        salary_str: str = (
            f"{salary_currency} {int(salary_min):,}–{int(salary_max):,}"
            f" {salary_period}".strip()
        )
    elif salary_min:
        salary_str = f"{salary_currency} {int(salary_min):,}+ {salary_period}".strip()
    else:
        salary_str = ""

    # ── Remote detection ──────────────────────────────────────────────────────
    is_remote: bool = bool(raw.get("job_is_remote", False))

    # ── Source portal ─────────────────────────────────────────────────────────
    # JSearch aggregates from multiple portals — track which one
    publisher: str = raw.get("job_publisher") or "JSearch"
    source_map: dict = {
        "linkedin":    "LinkedIn",
        "indeed":      "Indeed",
        "naukri":      "Naukri",
        "glassdoor":   "Glassdoor",
        "internshala": "Internshala",
    }
    source: str = "JSearch"
    for key, val in source_map.items():
        if key in publisher.lower():
            source = val
            break

    # ── Employment type ───────────────────────────────────────────────────────
    employment_type: str = raw.get("job_employment_type") or "FULLTIME"

    return {
        "job_id":          raw.get("job_id", ""),
        "title":           title,
        "company":         company,
        "location":        location,
        "description":     description,
        "job_url":         job_url,
        "salary":          salary_str,
        "salary_min":      salary_min,
        "salary_max":      salary_max,
        "salary_currency": salary_currency,
        "is_remote":       is_remote,
        "employment_type": employment_type,
        "source":          source,
        "publisher":       publisher,
        "posted_at":       raw.get("job_posted_at_datetime_utc", ""),
        "required_skills": raw.get("job_required_skills") or [],
        "highlights":      raw.get("job_highlights") or {},
    }