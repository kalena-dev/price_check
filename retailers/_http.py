"""Shared httpx client with realistic browser-like headers.

Used by every retailer adapter so we don't repeat header config 5 times.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


# Pretend to be a fresh-ish desktop Chrome on Windows (Quebec en-CA / fr-CA).
# Many retailer Cloudflare rules whitelist real browser UA + Accept-Language pairs.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8,fr;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def make_client(
    *,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    follow_redirects: bool = True,
) -> httpx.Client:
    """Build an httpx.Client preconfigured for retailer scraping."""
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Client(
        headers=headers,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )
