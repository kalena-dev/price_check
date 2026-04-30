"""Best Buy CA adapter.

Strategy: public JSON search endpoint at
https://www.bestbuy.ca/api/v2/json/search — returns a structured list of
products without HTML parsing. Cloudflare may challenge anonymous traffic,
in which case we raise RetailerBlockedError and the watcher logs+skips.

Category 20352 is "Gaming Laptops". We restrict the query to that category to
keep results relevant.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.bestbuy.ca/api/v2/json/search"
GAMING_LAPTOPS_CATEGORY = "20352"
PAGE_SIZE = 24


# Best Buy CA expects browser-like Accept + Referer.
_BB_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bestbuy.ca/",
    "Origin": "https://www.bestbuy.ca",
}


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


class BestBuyCA(Retailer):
    name = "bestbuy_ca"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        listings: list[Listing] = []
        seen_skus: set[str] = set()
        with make_client(extra_headers=_BB_HEADERS) as client:
            for query in cpu_filter:
                try:
                    listings.extend(self._search_one(client, query, seen_skus))
                except RetailerBlockedError:
                    # First block — abandon this retailer for the run.
                    raise
                except Exception as e:
                    logger.warning("bestbuy_ca: query %r failed: %s", query, e)
        return listings

    def _search_one(self, client, query: str, seen_skus: set[str]) -> list[Listing]:
        params = {
            "categoryid": GAMING_LAPTOPS_CATEGORY,
            "query": query,
            "page": 1,
            "pageSize": PAGE_SIZE,
            "lang": "en-CA",
        }
        resp = client.get(SEARCH_URL, params=params)

        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(
                f"bestbuy_ca returned HTTP {resp.status_code} (likely Cloudflare)"
            )
        if resp.status_code != 200:
            logger.warning("bestbuy_ca: HTTP %s for %r", resp.status_code, query)
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.warning("bestbuy_ca: non-JSON response for %r: %s", query, e)
            return []

        products = data.get("products", []) if isinstance(data, dict) else []
        out: list[Listing] = []
        for p in products:
            try:
                listing = self._parse_product(p)
            except Exception as e:
                logger.debug("bestbuy_ca: skipping product, parse error: %s", e)
                continue
            if listing is None:
                continue
            if listing.sku in seen_skus:
                continue
            seen_skus.add(listing.sku)
            out.append(listing)
        return out

    def _parse_product(self, p: dict) -> Listing | None:
        title = p.get("name") or ""
        cpu = normalize_cpu(title)
        if cpu is None:
            return None

        sku = str(p.get("sku") or "")
        if not sku:
            return None

        # Best Buy returns price as a number, sometimes nested in priceWithoutEhf etc.
        # Prefer salePrice when set, fall back to regularPrice.
        price_value = p.get("salePrice") or p.get("regularPrice") or p.get("price")
        if price_value is None:
            return None
        try:
            price = Decimal(str(price_value))
        except Exception:
            return None
        if price <= 0:
            return None

        product_url = p.get("productUrl") or ""
        if product_url and product_url.startswith("/"):
            product_url = "https://www.bestbuy.ca" + product_url

        image_url = p.get("thumbnailImage") or p.get("highResImage")
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

        # Condition — Best Buy marketplace open-box has "isMarketplace" or
        # "openBox" flags depending on API version. Default to new.
        condition = "new"
        if p.get("isOpenBox") or p.get("openBox"):
            condition = "open_box"

        return Listing(
            retailer=self.name,
            sku=sku,
            url=product_url,
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(title),
            gpu=_parse_gpu(title),
            price_cad=price,
            image_url=image_url,
            condition=condition,
        )
