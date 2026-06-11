# app/scraper/naukri.py
# ─────────────────────────────────────────────────────────────────────────────
# Naukri.com job scraper.
#
# WHY Naukri over Indeed for India:
#   - Largest job portal in India by volume
#   - Less aggressive bot detection
#   - Better structured HTML — more reliable selectors
#   - Most Indian companies post here first
#
# FLOW:
#   1. Navigate to naukri.com/jobs-listings/<role>-jobs-in-<location>
#   2. Extract job cards from search results
#   3. For each card click through to detail page
#   4. Extract full job description
#   5. Return structured job dicts
# ─────────────────────────────────────────────────────────────────────────────

import logging
import re
from typing import Optional
from urllib.parse import quote

from app.scraper.base import BaseScraper

logger = logging.getLogger(__name__)

NAUKRI_BASE: str = "https://www.naukri.com"


class NaukriScraper(BaseScraper):
    """
    Scrapes job listings from Naukri.com.

    Usage:
        with NaukriScraper(headless=True) as scraper:
            jobs = scraper.search_jobs(
                role="Python Backend Developer",
                location="Bangalore",
                max_results=10,
            )
    """

    def search_jobs(
        self,
        role: str,
        location: str,
        max_results: int = 10,
    ) -> list[dict]:
        """
        Search Naukri for jobs matching role + location.

        Args:
            role:        Job title / keywords e.g. "Python Backend Developer"
            location:    City name e.g. "Bangalore"
            max_results: Max listings to return.

        Returns:
            List of structured job dicts.
        """
        jobs: list[dict] = []

        # ── Build Naukri search URL ────────────────────────────────────────────
        # Naukri uses a slug format: /python-backend-developer-jobs-in-bangalore
        role_slug:     str = _slugify(role)
        location_slug: str = _slugify(location)
        search_url:    str = (
            f"{NAUKRI_BASE}/{role_slug}-jobs-in-{location_slug}"
            if location.lower() not in ("remote", "india", "")
            else f"{NAUKRI_BASE}/{role_slug}-jobs"
        )

        logger.info(f"Naukri search: {role} in {location} → {search_url}")

        try:
            self.goto(search_url, wait_until="domcontentloaded")
            self._human_delay(2.0, 4.0)

            if self.is_captcha():
                logger.warning("CAPTCHA detected on Naukri search page.")
                return []

            # ── Extract job cards ─────────────────────────────────────────────
            job_cards: list[dict] = self._extract_job_cards()

            if not job_cards:
                logger.warning(
                    "No Naukri job cards found. "
                    "Trying fallback URL format..."
                )
                # Fallback: use query param format
                fallback_url: str = (
                    f"{NAUKRI_BASE}/jobs-listings/"
                    f"{role_slug}-jobs-in-{location_slug}"
                )
                self.goto(fallback_url, wait_until="domcontentloaded")
                self._human_delay(2.0, 3.0)
                job_cards = self._extract_job_cards()

            if not job_cards:
                logger.warning("No job cards found on Naukri.")
                return []

            logger.info(f"Naukri: found {len(job_cards)} job cards.")

            # ── Fetch full details for each card ──────────────────────────────
            for card in job_cards[:max_results]:
                try:
                    detail: Optional[dict] = self._fetch_job_detail(card)
                    if detail:
                        jobs.append(detail)
                    self._human_delay(1.5, 3.5)
                except Exception as e:
                    logger.warning(f"Failed to fetch Naukri job detail: {e}")
                    continue

        except Exception as e:
            logger.error(f"Naukri search failed: {e}")

        logger.info(f"Naukri: returning {len(jobs)} jobs for '{role}' in '{location}'")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  Job card extraction
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_job_cards(self) -> list[dict]:
        """
        Extract job listings from Naukri search results page.

        Naukri renders job cards as article elements with class
        'jobTuple' or inside a container with id 'listContainer'.
        """
        cards: list[dict] = []

        # Wait for job listings to render
        loaded: bool = (
            self.wait_for("article.jobTuple", timeout=8000) or
            self.wait_for(".job-listings-container", timeout=5000) or
            self.wait_for("[data-job-id]", timeout=5000)
        )

        if not loaded:
            logger.warning("Naukri job cards did not load in time.")
            return []

        # Try multiple selectors — Naukri changes its markup periodically
        elements = (
            self.page.query_selector_all("article.jobTuple") or
            self.page.query_selector_all("[data-job-id]") or
            self.page.query_selector_all(".jobTupleHeader")
        )

        for el in elements:
            try:
                # ── Extract title ─────────────────────────────────────────────
                title: str = (
                    self._safe_el_text(el, ".title") or
                    self._safe_el_text(el, "a.title") or
                    self._safe_el_text(el, "[class*='title']")
                )

                # ── Extract company ───────────────────────────────────────────
                company: str = (
                    self._safe_el_text(el, ".companyInfo .name") or
                    self._safe_el_text(el, ".comp-name") or
                    self._safe_el_text(el, "[class*='company']")
                )

                # ── Extract location ──────────────────────────────────────────
                location: str = (
                    self._safe_el_text(el, ".locWdth") or
                    self._safe_el_text(el, "[class*='location']") or
                    self._safe_el_text(el, ".location")
                )

                # ── Extract salary ────────────────────────────────────────────
                salary: str = (
                    self._safe_el_text(el, ".salary") or
                    self._safe_el_text(el, "[class*='salary']") or
                    ""
                )

                # ── Extract job URL ───────────────────────────────────────────
                job_url: str = ""
                link_el = (
                    el.query_selector("a.title") or
                    el.query_selector("a[href*='/job-listings']") or
                    el.query_selector("a[href*='naukri.com']")
                )
                if link_el:
                    href: str = link_el.get_attribute("href") or ""
                    job_url = href if href.startswith("http") else f"{NAUKRI_BASE}{href}"

                # ── Extract experience ────────────────────────────────────────
                experience: str = (
                    self._safe_el_text(el, ".experience") or
                    self._safe_el_text(el, "[class*='experience']") or
                    ""
                )

                if title and company:
                    cards.append({
                        "title":      title,
                        "company":    company,
                        "location":   location,
                        "salary":     salary,
                        "experience": experience,
                        "job_url":    job_url,
                    })

            except Exception as e:
                logger.debug(f"Naukri card extraction error: {e}")
                continue

        return cards

    # ─────────────────────────────────────────────────────────────────────────
    #  Job detail fetching
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_job_detail(self, card: dict) -> Optional[dict]:
        """
        Navigate to a Naukri job detail page and extract full description.

        Args:
            card: Basic job info from _extract_job_cards().

        Returns:
            Complete job dict with description, or None on failure.
        """
        job_url: str = card.get("job_url", "")

        if not job_url:
            # No URL — return card data without description
            return {
                **card,
                "description": "",
                "is_remote":   False,
                "source":      "Naukri",
            }

        try:
            self.goto(job_url, wait_until="domcontentloaded")
            self._human_delay(1.5, 3.0)

            if self.is_captcha():
                logger.warning(f"CAPTCHA on Naukri job page: {job_url}")
                return None

            # Wait for description
            self.wait_for(".job-desc", timeout=8000)

            # ── Extract description ───────────────────────────────────────────
            description: str = (
                self.safe_text(".job-desc") or
                self.safe_text("[class*='job-description']") or
                self.safe_text(".jd-desc") or
                ""
            )

            # ── Extract updated title/company from detail page ────────────────
            title: str   = self.safe_text(".jd-header-title") or card["title"]
            company: str = self.safe_text(".jd-header-comp-name") or card["company"]

            # ── Extract skills from detail page ───────────────────────────────
            skills_text: str = self.safe_text(".key-skill") or ""

            # ── Detect remote ─────────────────────────────────────────────────
            content_lower: str = (
                description + card.get("location", "") + skills_text
            ).lower()
            is_remote: bool = any(
                w in content_lower
                for w in ["remote", "work from home", "wfh", "anywhere in india"]
            )

            return {
                "title":       title,
                "company":     company,
                "location":    card.get("location", ""),
                "description": description,
                "job_url":     job_url,
                "salary":      card.get("salary", ""),
                "experience":  card.get("experience", ""),
                "is_remote":   is_remote,
                "source":      "Naukri",
            }

        except Exception as e:
            logger.warning(f"Naukri detail fetch failed for {job_url}: {e}")
            # Return basic card data even if detail fetch fails
            return {
                **card,
                "description": "",
                "is_remote":   False,
                "source":      "Naukri",
            }

    # ─────────────────────────────────────────────────────────────────────────
    #  Helper
    # ─────────────────────────────────────────────────────────────────────────

    def _safe_el_text(self, element, selector: str) -> str:
        """Safely get text from a child element within a parent element."""
        try:
            child = element.query_selector(selector)
            if child:
                return (child.inner_text() or "").strip()
        except Exception:
            pass
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Utility
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """
    Convert a string to a Naukri-compatible URL slug.

    e.g. "Python Backend Developer" → "python-backend-developer"
         "Bangalore"                → "bangalore"
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)    # Remove special chars
    text = re.sub(r"[\s_]+", "-", text)     # Spaces to hyphens
    text = re.sub(r"-+", "-", text)         # Collapse multiple hyphens
    return text.strip("-")