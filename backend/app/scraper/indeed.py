# app/scraper/indeed.py
# ─────────────────────────────────────────────────────────────────────────────
# Indeed job listing scraper.
#
# FLOW:
#   1. Navigate to indeed.co.in/jobs?q=<role>&l=<location>
#   2. Extract job cards from search results page
#   3. For each card, fetch the full job description
#   4. Return list of structured job dicts
#
# RATE LIMITING:
#   Max 2 pages per search, 1–3 second delay between requests.
#   This keeps us well within safe limits.
#
# NOTE:
#   Indeed changes its HTML structure periodically.
#   If selectors stop working, inspect the page and update them here.
#   The selectors below are current as of mid-2025.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional
from urllib.parse import urlencode, urljoin

from app.scraper.base import BaseScraper

logger = logging.getLogger(__name__)

INDEED_BASE: str = "https://in.indeed.com"


class IndeedScraper(BaseScraper):
    """
    Scrapes job listings from Indeed India.

    Usage:
        with IndeedScraper(headless=True) as scraper:
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
        Search for jobs and return structured listings.

        Args:
            role:        Job title / keywords e.g. "Python Backend Developer"
            location:    City or "Remote" e.g. "Bangalore"
            max_results: Maximum number of job listings to return.

        Returns:
            List of job dicts with keys:
                title, company, location, description,
                job_url, salary, is_remote, source
        """
        jobs: list[dict] = []

        # ── Build search URL ──────────────────────────────────────────────────
        params: dict = {
            "q": role,
            "l": location,
            "sort": "date",   # Newest first
        }
        search_url: str = f"{INDEED_BASE}/jobs?{urlencode(params)}"

        logger.info(f"Indeed search: {role} in {location}")

        try:
            self.goto(search_url)

            # Check for CAPTCHA immediately
            if self.is_captcha():
                logger.warning("CAPTCHA detected on Indeed search page.")
                return []

            # ── Extract job cards from search results ─────────────────────────
            job_cards = self._extract_job_cards()

            if not job_cards:
                logger.warning("No job cards found. Indeed may have changed its HTML.")
                return []

            # ── Fetch full description for each card ──────────────────────────
            for card in job_cards[:max_results]:
                try:
                    detail = self._fetch_job_detail(card)
                    if detail:
                        jobs.append(detail)
                    self._human_delay(1.0, 3.0)   # Be polite between requests
                except Exception as e:
                    logger.warning(f"Failed to fetch job detail: {e}")
                    continue

        except Exception as e:
            logger.error(f"Indeed search failed: {e}")

        logger.info(f"Indeed: found {len(jobs)} jobs for '{role}' in '{location}'")
        return jobs

    def _extract_job_cards(self) -> list[dict]:
        """
        Extract basic job info from the search results page.

        Returns list of dicts with title, company, location, job_url.
        These are used to fetch full descriptions in the next step.
        """
        cards: list[dict] = []

        # Wait for job cards to load
        if not self.wait_for("[data-jk]", timeout=10000):
            logger.warning("Job cards did not load.")
            return []

        # Find all job card elements
        elements = self.page.query_selector_all("[data-jk]")

        for el in elements:
            try:
                # Extract job key (used in URL)
                job_key: str = el.get_attribute("data-jk") or ""
                if not job_key:
                    continue

                # Extract basic fields
                title: str   = self._safe_el_text(el, "[data-testid='jobTitle']")   \
                               or self._safe_el_text(el, ".jobTitle")
                company: str = self._safe_el_text(el, "[data-testid='company-name']") \
                               or self._safe_el_text(el, ".companyName")
                location: str = self._safe_el_text(el, "[data-testid='text-location']") \
                                or self._safe_el_text(el, ".companyLocation")
                salary: str  = self._safe_el_text(el, "[data-testid='attribute_snippet_testid']")

                job_url: str = f"{INDEED_BASE}/viewjob?jk={job_key}"

                if title and company:
                    cards.append({
                        "title":    title,
                        "company":  company,
                        "location": location,
                        "salary":   salary,
                        "job_url":  job_url,
                        "job_key":  job_key,
                    })

            except Exception as e:
                logger.debug(f"Card extraction error: {e}")
                continue

        return cards

    def _fetch_job_detail(self, card: dict) -> Optional[dict]:
        """
        Navigate to a job's detail page and extract the full description.

        Args:
            card: Basic job info dict from _extract_job_cards().

        Returns:
            Complete job dict including description, or None on failure.
        """
        try:
            self.goto(card["job_url"])

            if self.is_captcha():
                logger.warning(f"CAPTCHA on job page: {card['job_url']}")
                return None

            # Wait for description to load
            self.wait_for("#jobDescriptionText", timeout=8000)

            # Extract full description
            description: str = self.safe_text("#jobDescriptionText")

            # Try to get more details from the detail page
            title:   str = self.safe_text("[data-testid='jobsearch-JobInfoHeader-title']") \
                           or card["title"]
            company: str = self.safe_text("[data-testid='inlineHeader-companyName']") \
                           or card["company"]

            # Detect remote
            content_lower: str = (description + card.get("location", "")).lower()
            is_remote: bool    = any(w in content_lower for w in
                                     ["remote", "work from home", "wfh"])

            return {
                "title":       title,
                "company":     company,
                "location":    card.get("location", ""),
                "description": description,
                "job_url":     card["job_url"],
                "salary":      card.get("salary", ""),
                "is_remote":   is_remote,
                "source":      "Indeed",
            }

        except Exception as e:
            logger.warning(f"Detail fetch failed for {card.get('job_url')}: {e}")
            return None

    def _safe_el_text(self, element, selector: str) -> str:
        """
        Safely get text from a child element within a parent element.
        Returns empty string if not found.
        """
        try:
            child = element.query_selector(selector)
            if child:
                return (child.inner_text() or "").strip()
        except Exception:
            pass
        return ""