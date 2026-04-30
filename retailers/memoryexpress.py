"""Memory Express adapter.

Strategy: HTML scrape of search results page at
https://www.memoryexpress.com/Search/Products?Search=<query>&Category=LaptopGaming

ME's product cards expose `.c-shca-icon-item` blocks; they're stable enough.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from bs4 import BeautifulSoup

from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.memoryexpress.com/Search/Products"


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
    m = re.search(r"\b(\d{1,3})\s*GB\b", title, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_gpu(title: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?|GTX\s?\d{4})\b",
        title,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


class MemoryExpress(Retailer):
    name = "memoryexpress"

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
                    logger.warning("memoryexpress: query %r failed: %s", query, e)
        return listings

    def _search_one(self, client, query: str, seen_skus: set[str]) -> list[Listing]:
        params = {"Search": query, "Category": "LaptopGaming"}
        resp = client.get(SEARCH_URL, params=params)

        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(
                f"memoryexpress returned HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            logger.warning("memoryexpress: HTTP %s for %r", resp.status_code, query)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".c-shca-icon-item, .c-prod-grid__item")
        if not cards:
            return []

        out: list[Listing] = []
        for card in cards:
            try:
                listing = self._parse_card(card)
            except Exception as e:
                logger.debug("memoryexpress: skipping card, parse error: %s", e)
                continue
            if listing is None:
                continue
            if listing.sku in seen_skus:
                continue
            seen_skus.add(listing.sku)
            out.append(listing)
        return out

    def _parse_card(self, card) -> Listing | None:
        title_el = (
            card.select_one(".c-shca-icon-item__name a")
            or card.select_one(".c-shca-icon-item__name")
            or card.select_one("a.c-prod-grid__item-name")
        )
        if title_el is None:
            return None
        title = title_el.get_text(strip=True)
        url = title_el.get("href", "") if title_el.name == "a" else ""
        if not url:
            link = card.select_one("a")
            url = link.get("href", "") if link else ""
        if url.startswith("/"):
            url = "https://www.memoryexpress.com" + url

        cpu = normalize_cpu(title)
        if cpu is None:
            return None

        sku = card.get("data-product-id") or card.get("data-sku") or ""
        if not sku and url:
            m = re.search(r"/Products/([A-Z0-9\-]+)", url, re.IGNORECASE)
            if m:
                sku = m.group(1)
        if not sku:
            return None

        price_el = (
            card.select_one(".c-shca-icon-item__price")
            or card.select_one(".c-prod-grid__item-price")
        )
        price = _parse_price(price_el.get_text() if price_el else None)
        if price is None or price <= 0:
            return None

        img_el = card.select_one("img")
        image_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url and image_url.startswith("/"):
            image_url = "https://www.memoryexpress.com" + image_url

        condition = "new"
        if re.search(r"\bopen\s*box\b", title, re.IGNORECASE):
            condition = "open_box"

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
