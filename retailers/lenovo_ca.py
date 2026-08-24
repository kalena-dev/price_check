"""Lenovo CA adapter — Legion, LOQ, ThinkPad gaming/pro laptops.

Lenovo's search page is JS-rendered, so we drive it via Playwright. Each
product card has `data-product-code` (SKU) plus visible text containing
title, sale price, MSRP, and a CPU spec line. Prices on lenovo.com/ca
include MSRP markdowns that often beat retail aggregators.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from decimal import Decimal

from retailers._browser import PlaywrightUnavailable, browser_session
from retailers._components import parse_system_ram
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_BASE = "https://www.lenovo.com/ca/en/search"

# Card text patterns. Lenovo emits a multi-line block per card with a
# predictable shape — we anchor on price + spec lines.
_PRICE_RE = re.compile(r"\$([0-9][0-9,]*\.?\d{0,2})")


def _parse_decimal(text: str) -> Decimal | None:
    if not text:
        return None
    text = text.replace(",", "")
    try:
        return Decimal(text)
    except Exception:
        return None


def _parse_ram(text: str) -> int | None:
    return parse_system_ram(text)


def _parse_gpu(text: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?)\b",
        text,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


class LenovoCA(Retailer):
    name = "lenovo_ca"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        try:
            return self._search(cpu_filter)
        except PlaywrightUnavailable as e:
            raise RetailerBlockedError(f"lenovo_ca: {e}") from e

    def _search(self, cpu_filter: list[str]) -> list[Listing]:
        # Lenovo's search returns products across multiple categories. Rather
        # than one query per CPU, we run a few broad queries and let the CPU
        # normalizer filter — fewer page loads = faster + politer.
        broad_queries = ["Ryzen 9", "Ryzen AI", "Core Ultra"]

        listings: list[Listing] = []
        seen_skus: set[str] = set()

        with browser_session() as ctx:
            page = ctx.new_page()
            for q in broad_queries:
                url = f"{SEARCH_BASE}?{urllib.parse.urlencode({'text': q})}"
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.warning("lenovo_ca: navigation failed for %r: %s", q, e)
                    continue
                if resp is None or resp.status >= 500 or resp.status in (403, 429):
                    logger.warning("lenovo_ca: HTTP %s for %r", resp.status if resp else None, q)
                    continue
                # Wait for SPA to render product cards.
                try:
                    page.wait_for_selector("[data-product-code]", timeout=10000)
                except Exception:
                    logger.debug("lenovo_ca: no product cards rendered for %r", q)
                    continue

                cards = page.locator("[data-product-code]")
                n = cards.count()
                for i in range(n):
                    try:
                        card = cards.nth(i)
                        code = card.get_attribute("data-product-code") or ""
                        if not code:
                            continue
                        if code in seen_skus:
                            continue
                        text = card.inner_text()
                        listing = self._parse_card(code, text, card)
                    except Exception as e:
                        logger.debug("lenovo_ca: card %d parse error: %s", i, e)
                        continue
                    if listing is None:
                        continue
                    seen_skus.add(listing.sku)
                    listings.append(listing)
        return listings

    def _parse_card(self, code: str, text: str, card_locator) -> Listing | None:
        cpu = normalize_cpu(text)
        if cpu is None:
            return None

        # Title is typically the first non-empty line that isn't a tag like
        # "NEW ARRIVAL" / "FEATURED DEAL" / "Compare".
        title = self._extract_title(text)
        if not title:
            return None

        price = self._extract_price(text)
        if price is None or price <= 0 or price < Decimal("400"):
            return None

        # URL: try to read the anchor tag inside the card.
        url = ""
        try:
            href = card_locator.locator("a").first.get_attribute("href")
            if href:
                url = href if href.startswith("http") else f"https://www.lenovo.com{href}"
        except Exception:
            pass
        if not url:
            url = f"https://www.lenovo.com/ca/en/p/{code}"

        return Listing(
            retailer=self.name,
            sku=code,
            url=url,
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(text),
            gpu=_parse_gpu(text),
            price_cad=price,
            image_url=None,
            condition="new",
        )

    @staticmethod
    def _extract_title(text: str) -> str | None:
        skip = {
            "compare", "new arrival", "featured deal",
            "custom build", "best seller", "save",
        }
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if any(low == s or low.startswith(s + " ") for s in skip):
                continue
            if line.startswith("|"):
                continue
            if "$" in line:
                continue
            if low.startswith("custom build"):
                continue
            return line
        return None

    @staticmethod
    def _extract_price(text: str) -> Decimal | None:
        """Return the SALE price, ignoring crossed-out MSRP.

        Lenovo emits 'Est Value $X' for MSRP and the sale price on a separate
        line. The sale price typically follows the MSRP line. Strategy:
        find all $ amounts; if there's an "Est Value" preceding one, treat
        the NEXT $ amount as the sale price; else use the first $ amount.
        """
        prices = list(_PRICE_RE.finditer(text))
        if not prices:
            return None
        # Look for "Est Value" followed by a price; then the sale is the next.
        est_idx = text.lower().find("est value")
        if est_idx >= 0:
            after_est = [m for m in prices if m.start() > est_idx]
            # First match after est_idx is MSRP; sale is the second.
            if len(after_est) >= 2:
                return _parse_decimal(after_est[1].group(1))
        # Fallback: first $ amount on the card.
        return _parse_decimal(prices[0].group(1))
