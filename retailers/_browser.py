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
def browser_session(locale: str = "en-CA") -> Iterator:
    """Yield a Playwright BrowserContext. Closes on exit.

    Use:
        with browser_session() as ctx:
            page = ctx.new_page()
            page.goto(...)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise PlaywrightUnavailable(
            "playwright package not installed; run: pip install playwright && playwright install chromium"
        ) from e

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            raise PlaywrightUnavailable(
                f"chromium browser not available; run: playwright install chromium ({e})"
            ) from e
        ctx = browser.new_context(user_agent=_USER_AGENT, locale=locale)
        try:
            yield ctx
        finally:
            ctx.close()
            browser.close()
