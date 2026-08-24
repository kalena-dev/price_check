"""Best Buy CA gaming-desktop/prebuilt adapter."""

from __future__ import annotations

import logging
import re
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

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.bestbuy.ca/api/v2/json/search"
GAMING_DESKTOP_CATEGORY = "30441"
PAGE_SIZE = 24
MAX_PAGES_PER_QUERY = 2
SEARCH_QUERIES = (
    "RTX 50 gaming desktop",
    "RTX 40 gaming desktop",
    "Radeon RX gaming desktop",
)
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bestbuy.ca/",
    "Origin": "https://www.bestbuy.ca",
}


class BestBuyPrebuilts(Retailer):
    name = "bestbuy_prebuilts"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        del cpu_filter  # Desktop CPUs have a separate normalizer/allowlist.
        listings: list[Listing] = []
        seen: set[str] = set()
        with make_client(extra_headers=_HEADERS) as client:
            for query_index, query in enumerate(SEARCH_QUERIES):
                if query_index:
                    time.sleep(0.3)
                for page in range(1, MAX_PAGES_PER_QUERY + 1):
                    try:
                        chunk, has_more = self._search_page(
                            client, query, page, seen
                        )
                    except RetailerBlockedError:
                        if listings:
                            logger.warning(
                                "bestbuy_prebuilts: returning %d partial results "
                                "after rate limit",
                                len(listings),
                            )
                            return listings
                        raise
                    listings.extend(chunk)
                    if not has_more:
                        break
        return listings

    def _search_page(
        self,
        client,
        query: str,
        page: int,
        seen: set[str],
    ) -> tuple[list[Listing], bool]:
        resp = client.get(
            SEARCH_URL,
            params={
                "categoryid": GAMING_DESKTOP_CATEGORY,
                "query": query,
                "page": page,
                "pageSize": PAGE_SIZE,
                "lang": "en-CA",
            },
        )
        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(
                f"bestbuy_prebuilts returned HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            logger.warning(
                "bestbuy_prebuilts: HTTP %s for %r page %d",
                resp.status_code, query, page,
            )
            return [], False
        try:
            data = resp.json()
        except Exception as exc:
            raise RetailerParseError(
                f"Best Buy prebuilt search returned invalid JSON: {exc}"
            ) from exc
        products = data.get("products") if isinstance(data, dict) else None
        if not isinstance(products, list):
            raise RetailerParseError("Best Buy prebuilt products shape changed")

        out: list[Listing] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            try:
                listing = self._parse_product(product)
            except Exception as exc:
                logger.debug("bestbuy_prebuilts: parse error: %s", exc)
                continue
            if listing is None or listing.sku in seen:
                continue
            seen.add(listing.sku)
            out.append(listing)

        try:
            total_pages = int(data.get("totalPages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        return out, page < total_pages

    def _parse_product(self, product: dict) -> Listing | None:
        title = str(product.get("name") or "").strip()
        description = str(product.get("shortDescription") or "")
        text = f"{title} {description}"
        if not title or not looks_like_prebuilt(title):
            return None

        cpu = normalize_desktop_cpu(text)
        gpu = normalize_gpu(text)
        # A complete value estimate needs at least a known CPU. Gaming-desktop
        # searches are required to expose a discrete GPU as well.
        if cpu is None or gpu is None:
            return None

        sku = str(product.get("sku") or "")
        if not sku:
            return None
        price_value = (
            product.get("salePrice")
            or product.get("regularPrice")
            or product.get("price")
        )
        try:
            price = Decimal(str(price_value))
        except Exception:
            return None
        if price < Decimal("500"):
            return None

        url = str(product.get("productUrl") or "")
        if url.startswith("/"):
            url = "https://www.bestbuy.ca" + url
        image_url = product.get("highResImage") or product.get("thumbnailImage")
        image_url = str(image_url) if image_url else None
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

        condition = "new"
        if re.search(r"\bopen[\s-]*box\b|\bb[\s-]*stock\b", title, re.I):
            condition = "open_box"
        elif re.search(r"\brefurb|\brenewed\b", title, re.I):
            condition = "refurb"
        elif re.search(r"\bused\b|\bpre[\s-]*owned\b", title, re.I):
            condition = "used"

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
            condition=condition,
            product_type="prebuilt",
        )
