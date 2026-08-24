"""Walmart Canada gaming-desktop/prebuilt adapter."""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from retailers._components import (
    looks_like_prebuilt,
    normalize_desktop_cpu,
    normalize_gpu,
    parse_system_ram,
)
from retailers._http import make_client
from retailers.base import Listing, Retailer, RetailerBlockedError, RetailerParseError
from retailers.walmart_ca import (
    SEARCH_URL,
    _condition,
    _extract_next_data,
    _iter_products,
    _parse_decimal,
    _search_result,
)

logger = logging.getLogger(__name__)


SEARCH_QUERIES = (
    "prebuilt gaming PC",
    "RTX 5070 desktop",
    "Radeon RX gaming desktop",
)
MIN_PREBUILT_PRICE = Decimal("500")


class WalmartPrebuilts(Retailer):
    name = "walmart_prebuilts"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        del cpu_filter
        listings: list[Listing] = []
        seen: set[str] = set()
        with make_client(timeout=30.0) as client:
            for index, query in enumerate(SEARCH_QUERIES):
                if index:
                    time.sleep(0.5)
                try:
                    chunk = self._search_page(client, query, seen)
                except RetailerBlockedError:
                    if listings:
                        logger.warning(
                            "walmart_prebuilts: returning %d partial results "
                            "after rate limit",
                            len(listings),
                        )
                        return listings
                    raise
                listings.extend(chunk)
        return listings

    def _search_page(self, client, query: str, seen: set[str]) -> list[Listing]:
        resp = client.get(SEARCH_URL, params={"q": query})
        final_url = str(getattr(resp, "url", ""))
        if resp.status_code in (403, 429, 503) or "/blocked" in final_url:
            raise RetailerBlockedError(
                f"walmart_prebuilts returned HTTP {resp.status_code} or a block page"
            )
        if resp.status_code != 200:
            logger.warning(
                "walmart_prebuilts: HTTP %s for %r", resp.status_code, query
            )
            return []

        data = _extract_next_data(resp.text)
        result = _search_result(data) if data is not None else None
        if result is None:
            raise RetailerParseError(
                "Walmart prebuilt search no longer contains expected results"
            )

        out: list[Listing] = []
        for item in _iter_products(result):
            try:
                listing = self._parse_product(item)
            except Exception as exc:
                logger.debug("walmart_prebuilts: parse error: %s", exc)
                continue
            if listing is None or listing.sku in seen:
                continue
            seen.add(listing.sku)
            out.append(listing)
        return out

    def _parse_product(self, item: dict) -> Listing | None:
        title = str(item.get("name") or "").strip()
        description = str(item.get("shortDescription") or "")
        text = f"{title} {description}"
        if not title or not looks_like_prebuilt(title):
            return None

        cpu = normalize_desktop_cpu(text)
        gpu = normalize_gpu(text)
        if cpu is None or gpu is None:
            return None

        sku = str(item.get("usItemId") or "").strip()
        if not sku:
            return None
        availability = str(
            item.get("availabilityStatusDisplayValue")
            or item.get("availabilityStatus")
            or ""
        ).lower().replace("_", " ")
        if "out of stock" in availability or "unavailable" in availability:
            return None

        price = _parse_decimal(item.get("price"))
        if price is None:
            price_info = item.get("priceInfo") or {}
            if isinstance(price_info, dict):
                price = _parse_decimal(
                    price_info.get("linePrice")
                    or price_info.get("linePriceDisplay")
                )
        if price is None or price < MIN_PREBUILT_PRICE:
            return None

        url = str(item.get("canonicalUrl") or "")
        if url.startswith("/"):
            url = "https://www.walmart.ca" + url
        if not url:
            url = f"https://www.walmart.ca/en/ip/{sku}"

        image_url = item.get("image")
        if not image_url:
            image_info = item.get("imageInfo") or {}
            if isinstance(image_info, dict):
                image_url = image_info.get("thumbnailUrl")
        image_url = str(image_url) if image_url else None
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

        return Listing(
            retailer=self.name,
            sku=sku,
            url=url,
            title=title,
            cpu=cpu,
            ram_gb=parse_system_ram(text),
            gpu=gpu,
            price_cad=price,
            image_url=image_url,
            condition=_condition(item, title),
            product_type="prebuilt",
        )
