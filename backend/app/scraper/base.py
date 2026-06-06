# app/scraper/base.py
# ─────────────────────────────────────────────────────────────────────────────
# Base scraper — wraps Playwright with anti-detection measures.
#
# ANTI-DETECTION STRATEGY:
#   1. Rotate user-agent strings (looks like different browsers)
#   2. Random delays between actions (human-like timing)
#   3. Disable WebDriver flag (Playwright sets this by default — sites detect it)
#   4. Realistic viewport sizes
#   5. No headless indicator in navigator object
#
# USAGE (in subclasses):
#   class IndeedScraper(BaseScraper):
#       def scrape_jobs(self, query, location):
#           self.goto("https://indeed.com/jobs?q=...")
#           jobs = self.page.query_selector_all(".job_seen_beacon")
#           ...
# ─────────────────────────────────────────────────────────────────────────────

import random
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Rotating user agents ──────────────────────────────────────────────────────
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# ── Realistic viewport sizes ──────────────────────────────────────────────────
VIEWPORTS: list[dict] = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]


class BaseScraper:
    """
    Base class for all portal scrapers.
    Manages Playwright browser lifecycle and anti-detection.

    Usage:
        scraper = IndeedScraper()
        scraper.start()
        jobs = scraper.scrape_jobs("Python developer", "Bangalore")
        scraper.stop()

    Or as context manager:
        with IndeedScraper() as scraper:
            jobs = scraper.scrape_jobs(...)
    """

    def __init__(self, headless: bool = True):
        """
        Args:
            headless: Run browser without UI (True for production).
                      Set False during development to watch the browser.
        """
        self.headless: bool          = headless
        self._playwright             = None
        self._browser                = None
        self._context                = None
        self.page                    = None
        self._user_agent: str        = random.choice(USER_AGENTS)
        self._viewport: dict         = random.choice(VIEWPORTS)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the browser and create a new page."""
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # Hide automation
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        # New browser context — isolated cookies/storage per scrape session
        self._context = self._browser.new_context(
            user_agent=self._user_agent,
            viewport=self._viewport,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            # Mask WebDriver property that sites use to detect bots
            java_script_enabled=True,
        )

        # Inject anti-detection script on every page load
        self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
        """)

        self.page = self._context.new_page()
        logger.info(f"Browser started. UA: {self._user_agent[:50]}...")

    def stop(self) -> None:
        """Close browser and release all resources."""
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error stopping browser: {e}")
        finally:
            self._playwright = None
            self._browser    = None
            self._context    = None
            self.page        = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    # ── Navigation helpers ────────────────────────────────────────────────────

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """
        Navigate to a URL with timeout handling.

        Args:
            url:        The URL to navigate to.
            wait_until: When to consider navigation done.
                        "domcontentloaded" is faster than "networkidle".
        """
        try:
            self.page.goto(url, wait_until=wait_until, timeout=30000)
            self._human_delay(1.0, 2.5)   # Wait like a human would
        except Exception as e:
            logger.warning(f"Navigation failed for {url}: {e}")
            raise

    def safe_text(self, selector: str, default: str = "") -> str:
        """
        Safely extract text from a CSS selector.
        Returns default if element not found.

        Args:
            selector: CSS selector string.
            default:  Value to return if element doesn't exist.
        """
        try:
            element = self.page.query_selector(selector)
            if element:
                return (element.inner_text() or "").strip()
        except Exception:
            pass
        return default

    def safe_attr(self, selector: str, attr: str, default: str = "") -> str:
        """
        Safely get an attribute from a CSS selector element.

        Args:
            selector: CSS selector string.
            attr:     Attribute name e.g. "href", "src".
            default:  Value to return if not found.
        """
        try:
            element = self.page.query_selector(selector)
            if element:
                return element.get_attribute(attr) or default
        except Exception:
            pass
        return default

    def wait_for(self, selector: str, timeout: int = 10000) -> bool:
        """
        Wait for an element to appear on the page.

        Args:
            selector: CSS selector to wait for.
            timeout:  Maximum wait time in milliseconds.

        Returns:
            bool: True if element appeared, False if timed out.
        """
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            return False

    def is_captcha(self) -> bool:
        """Detect CAPTCHA or bot-challenge pages."""
        try:
            content: str = (self.page.content() or "").lower()
            url: str     = (self.page.url or "").lower()

            captcha_signals: list[str] = [
                "captcha", "recaptcha", "hcaptcha",
                "are you a human", "verify you are human",
                "robot", "cloudflare", "just a moment",
                "unusual traffic", "automated", "blocked",
                "challenge", "security check",
            ]

            url_signals: list[str] = [
                "challenge", "security", "blocked",
            ]

            return (
                any(s in content for s in captcha_signals) or
                any(s in url for s in url_signals)
            )
        except Exception:
            return False

    # ── Anti-detection helpers ────────────────────────────────────────────────

    def _human_delay(
        self,
        min_seconds: float = 0.5,
        max_seconds: float = 2.0,
    ) -> None:
        """
        Sleep for a random duration to simulate human behaviour.
        Never use time.sleep(constant) — that's detectable.
        """
        delay: float = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)

    def _human_type(self, selector: str, text: str) -> None:
        """
        Type text character by character with random delays.
        Looks like a human typing instead of an instant paste.

        Args:
            selector: CSS selector of the input field.
            text:     Text to type.
        """
        try:
            self.page.click(selector)
            self._human_delay(0.2, 0.5)

            for char in text:
                self.page.keyboard.type(char)
                time.sleep(random.uniform(0.05, 0.15))  # 50–150ms per character

        except Exception as e:
            logger.warning(f"Human type failed on {selector}: {e}")
            # Fallback: direct fill (faster but more detectable)
            self.page.fill(selector, text)