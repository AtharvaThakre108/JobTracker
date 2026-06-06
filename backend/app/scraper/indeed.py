# app/scraper/indeed.py
# ─────────────────────────────────────────────────────────────────────────────
# Indeed India job scraper.
# ─────────────────────────────────────────────────────────────────────────────

import json
import logging
import os
from typing import Optional
from urllib.parse import urlencode

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

        params: dict = {"q": role, "l": location, "sort": "date"}
        search_url: str = f"{INDEED_BASE}/jobs?{urlencode(params)}"

        logger.info(f"Indeed search: {role} in {location}")

        try:
            # ── Load cookies ──────────────────────────────────────────────────────
            self._load_cookies()

            # ── Land on homepage first (looks more human) ─────────────────────────
            self.goto("https://in.indeed.com", wait_until="domcontentloaded")
            self._human_delay(2.0, 4.0)

            # ── Now navigate to search ────────────────────────────────────────────
            self.goto(search_url, wait_until="domcontentloaded")
            self._human_delay(2.0, 3.0)

            if self.is_captcha():
                logger.warning("CAPTCHA detected on Indeed search page.")
                return []

            # ── Fetch full description for each card ──────────────────────────
            for card in job_cards[:max_results]:
                try:
                    detail = self._fetch_job_detail(card)
                    if detail:
                        jobs.append(detail)
                    self._human_delay(1.0, 3.0)
                except Exception as e:
                    logger.warning(f"Failed to fetch job detail: {e}")
                    continue

        except Exception as e:
            logger.error(f"Indeed search failed: {e}")

        logger.info(f"Indeed: found {len(jobs)} jobs for '{role}' in '{location}'")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  Cookie management
    # ─────────────────────────────────────────────────────────────────────────

    def _load_cookies(self) -> bool:
        """
        Load saved Indeed session cookies into the browser context.

        Looks for indeed_cookies.json in the backend root folder.
        Export cookies using the Cookie-Editor browser extension after
        logging into Indeed manually.

        Returns:
            bool: True if cookies loaded, False if file not found.
        """
        cookie_path: str = os.path.join(
            os.path.dirname(                    # backend/
                os.path.dirname(                # app/
                    os.path.dirname(__file__)   # scraper/
                )
            ),
            "indeed_cookies.json",
        )

        if not os.path.exists(cookie_path):
            logger.warning("No indeed_cookies.json found. Running without session.")
            return False

        try:
            with open(cookie_path, "r") as f:
                cookies: list[dict] = json.load(f)

            formatted: list[dict] = []
            for c in cookies:
                cookie: dict = {
                    "name":   c.get("name", ""),
                    "value":  c.get("value", ""),
                    "domain": c.get("domain", ".indeed.com"),
                    "path":   c.get("path", "/"),
                }
                if c.get("secure") is not None:
                    cookie["secure"] = c["secure"]
                if c.get("httpOnly") is not None:
                    cookie["httpOnly"] = c["httpOnly"]
                if c.get("sameSite") in ("Strict", "Lax", "None"):
                    cookie["sameSite"] = c["sameSite"]

                formatted.append(cookie)

            self._context.add_cookies(formatted)
            logger.info(f"Loaded {len(formatted)} Indeed cookies.")
            return True

        except Exception as e:
            logger.warning(f"Failed to load Indeed cookies: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  Job card extraction
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_job_cards(self) -> list[dict]:
        """
        Extract basic job info from the search results page.
        Returns list of dicts with title, company, location, job_url.
        """
        cards: list[dict] = []

        if not self.wait_for("[data-jk]", timeout=10000):
            logger.warning("Job cards did not load.")
            return []

        elements = self.page.query_selector_all("[data-jk]")

        for el in elements:
            try:
                job_key: str = el.get_attribute("data-jk") or ""
                if not job_key:
                    continue

                title: str   = self._safe_el_text(el, "[data-testid='jobTitle']") \
                               or self._safe_el_text(el, ".jobTitle")
                company: str = self._safe_el_text(el, "[data-testid='company-name']") \
                               or self._safe_el_text(el, ".companyName")
                location: str = self._safe_el_text(el, "[data-testid='text-location']") \
                                or self._safe_el_text(el, ".companyLocation")
                salary: str  = self._safe_el_text(
                    el, "[data-testid='attribute_snippet_testid']"
                )

                if title and company:
                    cards.append({
                        "title":    title,
                        "company":  company,
                        "location": location,
                        "salary":   salary,
                        "job_url":  f"{INDEED_BASE}/viewjob?jk={job_key}",
                        "job_key":  job_key,
                    })

            except Exception as e:
                logger.debug(f"Card extraction error: {e}")
                continue

        return cards

    # ─────────────────────────────────────────────────────────────────────────
    #  Job detail fetching
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_job_detail(self, card: dict) -> Optional[dict]:
        """
        Navigate to a job detail page and extract the full description.

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

            self.wait_for("#jobDescriptionText", timeout=8000)

            description: str = self.safe_text("#jobDescriptionText")
            title: str       = self.safe_text(
                "[data-testid='jobsearch-JobInfoHeader-title']"
            ) or card["title"]
            company: str     = self.safe_text(
                "[data-testid='inlineHeader-companyName']"
            ) or card["company"]

            content_lower: str = (description + card.get("location", "")).lower()
            is_remote: bool    = any(
                w in content_lower
                for w in ["remote", "work from home", "wfh"]
            )

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