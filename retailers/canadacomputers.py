"""Canada Computers adapter.

Strategy: search via
https://www.canadacomputers.com/index.php?action=search&keywords=<query>
and parse `.product-desc-box` cards out of the resulting HTML.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from decimal import Decimal

from bs4 import BeautifulSoup

from retailers._components import parse_system_ram
from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.canadacomputers.com/index.php"

_LAPTOP_PATH_RE = re.compile(r"/(?:windows-laptops|gaming-laptops|macbooks|laptops)/(\d+)/", re.IGNORECASE)
_PRICE_TEXT_RE = re.compile(r"^\$[\d,]+(?:\.\d{1,2})?$")


def _parse_price(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _parse_ram(title: str) -> int | None:
    return parse_system_ram(title)


def _parse_gpu(title: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?|GTX\s?\d{4})\b",
        title,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _is_inside_class(el, classnames: tuple[str, ...]) -> bool:
    cur = el.parent
    while cur is not None:
        cls = cur.get("class") or []
        if any(c in cls for c in classnames):
            return True
        cur = cur.parent
    return False


def _extract_current_price(card) -> Decimal | None:
    """Find the current sticker price, ignoring strikethrough MSRP and savings labels."""
    # First preference: red sale-price color class.
    el = card.select_one("span.c-DA0000")
    if el and _PRICE_TEXT_RE.match(el.get_text(strip=True)):
        return _parse_price(el.get_text(strip=True))

    # Fallback: any span/div whose text is a clean "$X.XX", not inside savings or strikethrough.
    for el in card.find_all(["span", "div", "p"]):
        text = el.get_text(strip=True)
        if not _PRICE_TEXT_RE.match(text):
            continue
        if _is_inside_class(el, ("crasher-price2", "text-decoration-line-through")):
            continue
        return _parse_price(text)
    return None


class CanadaComputers(Retailer):
    name = "canadacomputers"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        listings: list[Listing] = []
        seen_skus: set[str] = set()
        with make_client() as client:
            for query in cpu_filter:
                try:
                    listings.extend(self._search_one(client, query, seen_skus))
                except RetailerBlockedError:
                    raise
                except Exception as e:
                    logger.warning("canadacomputers: query %r failed: %s", query, e)
        return listings

    def _search_one(self, client, query: str, seen_skus: set[str]) -> list[Listing]:
        params = {"action": "search", "keywords": query}
        url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
        resp = client.get(url)

        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(
                f"canadacomputers returned HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            logger.warning("canadacomputers: HTTP %s for %r", resp.status_code, query)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".product-desc-box")
        if not cards:
            return []

        out: list[Listing] = []
        for card in cards:
            try:
                listing = self._parse_card(card)
            except Exception as e:
                logger.debug("canadacomputers: skipping card, parse error: %s", e)
                continue
            if listing is None:
                continue
            if listing.sku in seen_skus:
                continue
            seen_skus.add(listing.sku)
            out.append(listing)
        return out

    def _parse_card(self, card) -> Listing | None:
        # Title — `.product-desc-title` exists; text is in nested span/div.
        title_el = card.select_one(".product-desc-title")
        if title_el is None:
            # Fallback: image alt text on the thumbnail link.
            img = card.select_one("a img")
            title = img.get("alt", "") if img else ""
        else:
            title = title_el.get_text(strip=True)
        if not title:
            return None

        cpu = normalize_cpu(title)
        if cpu is None:
            return None

        # Resolve product URL: prefer the product-desc anchor, else the thumbnail anchor.
        link = card.select_one("a.product-desc") or card.select_one("a")
        url = link.get("href", "") if link else ""
        if url.startswith("/"):
            url = "https://www.canadacomputers.com" + url

        # Filter: URL must be a laptop category. CC's category URLs include
        # "windows-laptops", "gaming-laptops", "macbooks", "laptops".
        m = _LAPTOP_PATH_RE.search(url)
        if m is None:
            return None
        sku = m.group(1)

        price = _extract_current_price(card)
        if price is None or price <= 0:
            return None

        img_el = card.select_one("img")
        image_url = (img_el.get("data-src") or img_el.get("src")) if img_el else None
        if image_url and image_url.startswith("data:"):
            image_url = img_el.get("data-src") if img_el else None
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

        condition = "new"
        if re.search(r"\bopen\s*box\b|b[\-\s]?stock\b", title, re.IGNORECASE):
            condition = "open_box"
        if re.search(r"\brefurb", title, re.IGNORECASE):
            condition = "refurb"

        return Listing(
            retailer=self.name,
            sku=sku,
            url=url,
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(title),
            gpu=_parse_gpu(title),
            price_cad=price,
            image_url=image_url,
            condition=condition,
        )
