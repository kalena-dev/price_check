"""Playwright-backed browser helper.

Used by retailers whose pages are JS-rendered (Lenovo, ASUS, etc.).
Wraps a single headless Chromium instance; each retailer gets its own
context with its own page.

Falls back gracefully if Playwright isn't installed: get_browser() raises
PlaywrightUnavailable so the watcher can log+skip without crashing.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterator

logger = logging.getLogger(__name__)


class PlaywrightUnavailable(Exception):
    """Raised when Playwright (or its browser) isn't available."""


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@contextlib.contextmanager
def browser_session(locale: str = "en-CA", stealth: bool = False) -> Iterator:
    """Yield a Playwright BrowserContext. Closes on exit.

    Set stealth=True to apply playwright_stealth patches per-page (helps with
    sites that detect headless browsers). Stealth import is lazy so the
    module isn't required for non-stealth retailers.

    Use:
        with browser_session(stealth=True) as ctx:
            page = ctx.new_page()
            page.goto(...)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise PlaywrightUnavailable(
            "playwright package not installed; run: pip install playwright && playwright install chromium"
        ) from e

    stealth_fn = None
    if stealth:
        try:
            from playwright_stealth import stealth_sync as stealth_fn  # type: ignore[no-redef]
        except ImportError:
            logger.warning("playwright_stealth not installed; proceeding without stealth")
            stealth_fn = None

    launch_args = ["--disable-blink-features=AutomationControlled"] if stealth else []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=launch_args)
        except Exception as e:
            raise PlaywrightUnavailable(
                f"chromium browser not available; run: playwright install chromium ({e})"
            ) from e
        ctx = browser.new_context(
            user_agent=_USER_AGENT,
            locale=locale,
            viewport={"width": 1920, "height": 1080} if stealth else None,
        )
        # Wrap new_page so stealth is applied automatically.
        if stealth_fn is not None:
            _orig_new_page = ctx.new_page
            def _new_page_with_stealth():
                page = _orig_new_page()
                stealth_fn(page)
                return page
            ctx.new_page = _new_page_with_stealth  # type: ignore[method-assign]
        try:
            yield ctx
        finally:
            ctx.close()
            browser.close()
