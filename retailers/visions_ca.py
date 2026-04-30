"""Visions Electronics CA adapter (Playwright + stealth).

Visions is a Canadian electronics chain with a gaming-laptops category page
that exposes 32+ product cards in a single shot. Each card is wrapped in
`.product-item-info` and contains a title, sale/MSRP prices, SKU, and
product URL.

Card text format (joined into one string):
    "Clearance Add to Wish ListAdd to Compare {TITLE} (SKU) ... Special Price
     $X,XXX.XX Regular Price $Y,YYY.YY ..."

If there's no sale, only one $ amount appears.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from retailers._browser import PlaywrightUnavailable, browser_session
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


GAMING_LAPTOPS_URL = (
    "https://www.visions.ca/shop/category/computers-home-office-accessories/"
    "laptops-chromebooks/gaming-laptops"
)

# Optional secondary categories — Visions splits laptops into multiple buckets.
ALL_LAPTOP_URLS = [
    GAMING_LAPTOPS_URL,
    # Home & office may catch business-class laptops with HX chips occasionally.
    "https://www.visions.ca/shop/category/computers-home-office-accessories/"
    "laptops-chromebooks/home-office-laptops",
]


_SKU_RE = re.compile(r"\(([A-Z0-9][A-Z0-9\-]+[A-Z0-9])\)")
_PRICE_RE = re.compile(r"\$([0-9][0-9,]*\.?\d{0,2})")


def _parse_decimal(s: str) -> Decimal | None:
    if not s:
        return None
    s = s.replace(",", "")
    try:
        d = Decimal(s)
    except Exception:
        return None
    return d if d > 0 else None


def _parse_ram(text: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*GB\b", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_gpu(text: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?)\b",
        text,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _extract_title(text: str) -> str | None:
    """Title sits between 'Add to Compare' and the SKU paren."""
    # Strip leading "Clearance " / etc.
    cleaned = re.sub(r"^(Clearance|New|Featured)\s+", "", text, flags=re.IGNORECASE)
    m = re.search(r"Add to Compare\s+(.+?)\s+\(", cleaned, re.DOTALL)
    if m:
        return m.group(1).strip().strip(".")
    # Fallback: first non-empty line
    for line in cleaned.splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("add to", "compare")):
            return line
    return None


def _extract_price(text: str) -> Decimal | None:
    """Pick the SALE price, ignoring 'Regular Price'."""
    # If there's an explicit "Special Price $X" form, that's the sale.
    m = re.search(r"Special\s+Price\s+\$([0-9,]+(?:\.\d{1,2})?)", text)
    if m:
        return _parse_decimal(m.group(1))
    # No sale — first $ amount in the card.
    m = re.search(r"\$([0-9,]+(?:\.\d{1,2})?)", text)
    return _parse_decimal(m.group(1)) if m else None


class VisionsCA(Retailer):
    name = "visions_ca"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        try:
            return self._search()
        except PlaywrightUnavailable as e:
            raise RetailerBlockedError(f"visions_ca: {e}") from e

    def _search(self) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()

        with browser_session(stealth=True) as ctx:
            page = ctx.new_page()
            for url in ALL_LAPTOP_URLS:
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.warning("visions_ca: navigation failed for %s: %s", url, e)
                    continue
                if resp is None or resp.status >= 500 or resp.status in (403, 429):
                    logger.warning(
                        "visions_ca: HTTP %s for %s",
                        resp.status if resp else None, url,
                    )
                    continue
                page.wait_for_timeout(4000)
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(2000)

                # Each unique product has multiple `[data-product-id]` el's;
                # the .product-item-info wrapper holds the full card.
                cards = page.locator(".product-item-info")
                n = cards.count()
                for i in range(n):
                    try:
                        card = cards.nth(i)
                        text = card.inner_text()
                        link = card.locator("a[href]").first
                        href = link.get_attribute("href") or ""
                    except Exception as e:
                        logger.debug("visions_ca: card %d read error: %s", i, e)
                        continue

                    listing = self._parse_card(text, href)
                    if listing is None:
                        continue
                    if listing.sku in seen:
                        continue
                    seen.add(listing.sku)
                    listings.append(listing)
        return listings

    def _parse_card(self, text: str, href: str) -> Listing | None:
        if not text:
            return None
        cpu = normalize_cpu(text)
        if cpu is None:
            return None

        title = _extract_title(text)
        if not title:
            return None

        price = _extract_price(text)
        if price is None or price <= 0 or price < Decimal("400"):
            return None

        # SKU — parens form, fallback to the URL slug tail.
        sku = ""
        m = _SKU_RE.search(text)
        if m:
            sku = m.group(1)
        if not sku and href:
            sku = href.rstrip("/").rsplit("/", 1)[-1] or href
        if not sku:
            return None

        url = href if href.startswith("http") else f"https://www.visions.ca{href}"

        condition = "new"
        if "clearance" in text.lower() or "open box" in text.lower():
            condition = "open_box"
        if "refurb" in text.lower():
            condition = "refurb"

        return Listing(
            retailer=self.name,
            sku=sku,
            url=url,
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(text),
            gpu=_parse_gpu(text),
            price_cad=price,
            image_url=None,
            condition=condition,
        )
