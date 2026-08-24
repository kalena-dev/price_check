"""Visions Electronics CA adapter (Playwright + stealth).

Visions is a Canadian electronics chain with a gaming-laptops category page
that exposes product cards in a single shot.

Title strategy (in order):
  1. The image alt attribute — full untruncated title (Visions truncates
     visible card text with CSS but keeps full title in <img alt>).
  2. If the alt text leaves the chip ambiguous ("Core Ultra 9" without an
     HX model, "Ryzen 9" without a digit), follow the URL to the product
     detail page and pull the exact chip from the spec body.

Card text format:
    "Clearance Add to Wish ListAdd to Compare {TITLE} (SKU) ... Special Price
     $X,XXX.XX Regular Price $Y,YYY.YY ..."

Cap on detail-page fetches per cycle (DETAIL_FETCH_BUDGET) keeps Visions
runs to a polite size even if many cards are vague.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from retailers._browser import PlaywrightUnavailable, browser_session
from retailers._components import parse_system_ram
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


GAMING_LAPTOPS_URL = (
    "https://www.visions.ca/shop/category/computers-home-office-accessories/"
    "laptops-chromebooks/gaming-laptops"
)
ALL_LAPTOP_URLS = [
    GAMING_LAPTOPS_URL,
    "https://www.visions.ca/shop/category/computers-home-office-accessories/"
    "laptops-chromebooks/home-office-laptops",
]

# How many ambiguous-chip detail pages we'll fetch per run, total. Each
# fetch is 1 extra navigation. Most cycles should hit 0–2 of these.
DETAIL_FETCH_BUDGET = 6


_SKU_RE = re.compile(r"\(([A-Z0-9][A-Z0-9\-]+[A-Z0-9])\)")

# Tier hints that, if present in the listing's alt text, indicate a chip
# family that *could* be on the watch list — worth a detail-page lookup.
# We still detect ambiguity from the alt, but on the detail page we just
# look for any normalized chip in the first ~3.5KB (the breadcrumb +
# title + spec area, before "you might also like" suggestions).
#
# Note: Visions' alt text is sometimes wrong (e.g. "Ultra9" for what is
# actually an Ultra 7 chip). We trust the detail page over the alt.
_AMBIGUOUS_CHIP_RE = re.compile(
    r"\b("
    r"core\s+ultra\s*\d(?!\s*\d{3})"               # "Core Ultra 9" w/o HX model
    r"|ryzen\s+(?:ai\s+)?\d(?!\s*\d)"               # "Ryzen 9", "Ryzen AI 9" w/o digit
    r"|ryzen\s+ai\s+max\+?(?!\s*(?:pro\s+)?\d)"     # "Ryzen AI Max+" w/o 3-digit
    r"|m[45]\s+(?:pro|max)(?!\s*(?:chip\s+with\s+)?\d{1,2}[\s\-]?core)"  # "M4 Max" w/o cores
    r")\b",
    re.IGNORECASE,
)

# Detail-page chip search bounds — only look in the spec area, not in the
# product-recommendations carousel that comes after.
_DETAIL_TEXT_LIMIT = 3500


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
    return parse_system_ram(text)


def _parse_gpu(text: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?)\b",
        text,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _extract_title_from_card_text(text: str) -> str | None:
    """Title sits between 'Add to Compare' and the SKU paren in card text."""
    cleaned = re.sub(r"^(Clearance|New|Featured)\s+", "", text, flags=re.IGNORECASE)
    m = re.search(r"Add to Compare\s+(.+?)\s+\(", cleaned, re.DOTALL)
    if m:
        return m.group(1).strip().strip(".")
    for line in cleaned.splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("add to", "compare")):
            return line
    return None


def _extract_price(text: str) -> Decimal | None:
    """Pick the SALE price, ignoring 'Regular Price'."""
    m = re.search(r"Special\s+Price\s+\$([0-9,]+(?:\.\d{1,2})?)", text)
    if m:
        return _parse_decimal(m.group(1))
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
        detail_fetches_left = DETAIL_FETCH_BUDGET

        with browser_session(stealth=True) as ctx:
            page = ctx.new_page()
            detail_page = ctx.new_page()
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

                # Pull out (alt, href, card_text) tuples for every product card.
                # Visions' visible card text is CSS-truncated; <img alt> has the
                # full title. We use alt for normalization; card text for price/SKU.
                rows = page.evaluate("""() => {
                    const cards = document.querySelectorAll('.product-item-info');
                    const out = [];
                    for (const card of cards) {
                        const img = card.querySelector('img[alt]');
                        const link = card.querySelector('a[href*="visions.ca/"]:not([href*="category"])');
                        if (!img || !link) continue;
                        out.push({
                            alt: img.getAttribute('alt') || '',
                            href: link.href || '',
                            text: card.innerText || '',
                        });
                    }
                    return out;
                }""")

                for row in rows:
                    listing, used_budget = self._parse_row(
                        row.get("alt", ""),
                        row.get("href", ""),
                        row.get("text", ""),
                        detail_page if detail_fetches_left > 0 else None,
                    )
                    if used_budget:
                        detail_fetches_left -= 1
                    if listing is None:
                        continue
                    if listing.sku in seen:
                        continue
                    seen.add(listing.sku)
                    listings.append(listing)
        return listings

    def _parse_row(
        self,
        alt: str,
        href: str,
        card_text: str,
        detail_page,
    ) -> tuple[Listing | None, bool]:
        """Returns (listing, used_detail_fetch)."""
        if not alt:
            return None, False

        # Try the full alt text first.
        cpu = normalize_cpu(alt)

        # If alt is ambiguous about the chip and matches a tier family we
        # could plausibly care about, follow the detail page to get the
        # precise chip from the spec area.
        used_detail_fetch = False
        if cpu is None and detail_page is not None and href and _AMBIGUOUS_CHIP_RE.search(alt):
            cpu = self._cpu_from_detail_page(detail_page, href)
            used_detail_fetch = True

        if cpu is None:
            return None, used_detail_fetch

        price = _extract_price(card_text)
        if price is None or price <= 0 or price < Decimal("400"):
            return None, used_detail_fetch

        sku = ""
        m = _SKU_RE.search(card_text)
        if m:
            sku = m.group(1)
        if not sku and href:
            sku = href.rstrip("/").rsplit("/", 1)[-1] or href
        if not sku:
            return None, used_detail_fetch

        url = href if href.startswith("http") else f"https://www.visions.ca{href}"

        condition = "new"
        low = card_text.lower()
        if "clearance" in low or "open box" in low:
            condition = "open_box"
        if "refurb" in low:
            condition = "refurb"

        return Listing(
            retailer=self.name,
            sku=sku,
            url=url,
            title=alt,
            cpu=cpu,
            ram_gb=_parse_ram(alt) or _parse_ram(card_text),
            gpu=_parse_gpu(alt) or _parse_gpu(card_text),
            price_cad=price,
            image_url=None,
            condition=condition,
        ), used_detail_fetch

    def _cpu_from_detail_page(self, detail_page, href: str) -> str | None:
        """Navigate to the product detail page; return canonical CPU or None.

        We restrict the search to the first DETAIL_TEXT_LIMIT bytes — that's
        the breadcrumb + product title + spec area, before the
        "you might also like" carousel. Chips in suggestions appear later
        and shouldn't bleed into our identification.
        """
        try:
            resp = detail_page.goto(href, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            logger.debug("visions_ca: detail navigation failed (%s): %s", href, e)
            return None
        if resp is None or resp.status != 200:
            return None
        try:
            detail_page.wait_for_timeout(2500)
            text = detail_page.evaluate("() => document.body.innerText")
        except Exception as e:
            logger.debug("visions_ca: detail extract failed (%s): %s", href, e)
            return None
        return normalize_cpu(text[:_DETAIL_TEXT_LIMIT])
