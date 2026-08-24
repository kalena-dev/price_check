"""Best Buy CA adapter.

Strategy: public JSON search endpoint at
https://www.bestbuy.ca/api/v2/json/search — returns a structured list of
products without HTML parsing. Cloudflare may challenge anonymous traffic,
in which case we raise RetailerBlockedError and the watcher logs+skips.

Category 20352 is "Laptops & MacBooks". Searches are paginated, and products
whose result-card title omits the exact processor variant are enriched through
Best Buy's public product-detail JSON endpoint.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from bs4 import BeautifulSoup

from retailers._components import parse_system_ram
from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.bestbuy.ca/api/v2/json/search"
PRODUCT_URL = "https://www.bestbuy.ca/api/v2/json/product/{sku}"
LAPTOPS_CATEGORY = "20352"
PAGE_SIZE = 24
# Some short CPU searches are very fuzzy. Three pages substantially improve
# coverage without allowing one query to walk hundreds of irrelevant results.
MAX_PAGES_PER_QUERY = 3
MAX_DETAIL_FETCHES = 12

# Result titles sometimes say only "M4 Pro", "Core Ultra 9", or "Ryzen 9".
# Those products are worth one cached detail request to read their exact specs.
_AMBIGUOUS_CPU_RE = re.compile(
    r"\b(?:m[345]\s+(?:pro|max)|core\s+ultra\s+[579]|ryzen(?:\s+ai)?\s+[579])\b",
    re.IGNORECASE,
)


# Best Buy CA expects browser-like Accept + Referer.
_BB_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bestbuy.ca/",
    "Origin": "https://www.bestbuy.ca",
}


def _parse_ram(title: str) -> int | None:
    return parse_system_ram(title)


def _parse_gpu(title: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?|GTX\s?\d{4})\b",
        title,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _detail_spec_text(detail: dict) -> str:
    """Build normalization-friendly text from Best Buy's structured specs."""
    specs = detail.get("specs") or []
    values: dict[str, str] = {}
    all_values: list[str] = []
    if isinstance(specs, list):
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip().lower()
            value = str(spec.get("value") or "").strip()
            if value:
                all_values.append(value)
            if name and value:
                values[name] = value

    # Apple normalization needs the CPU core count next to the chip name.
    processor = values.get("processor type", "")
    cores = values.get("processor cores", "")
    processor_with_cores = processor
    if processor and cores:
        processor_with_cores = f"{processor} {cores}-core CPU"

    long_description = BeautifulSoup(
        str(detail.get("longDescription") or ""), "html.parser"
    ).get_text(" ", strip=True)
    return " ".join(
        part for part in (
            processor_with_cores,
            str(detail.get("name") or ""),
            " ".join(all_values),
            long_description,
        ) if part
    )


def _detail_is_sold_out(detail: dict) -> bool:
    availability = detail.get("availability") or {}
    if not isinstance(availability, dict):
        return False
    online = str(availability.get("onlineAvailability") or "").lower()
    in_store = str(availability.get("inStoreAvailability") or "").lower()
    return (
        online in {"soldout", "notavailable"}
        and in_store in {"notavailable", "notavailableatthislocation"}
        and not detail.get("isAvailableForPickup")
    )


class BestBuyCA(Retailer):
    name = "bestbuy_ca"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        listings: list[Listing] = []
        seen_skus: set[str] = set()
        detail_cache: dict[str, dict | None] = {}
        with make_client(extra_headers=_BB_HEADERS) as client:
            for query in cpu_filter:
                try:
                    listings.extend(
                        self._search_one(client, query, seen_skus, detail_cache)
                    )
                except RetailerBlockedError:
                    if listings:
                        logger.warning(
                            "bestbuy_ca: rate-limited after %d listings; "
                            "returning partial results",
                            len(listings),
                        )
                        return listings
                    raise
                except Exception as e:
                    logger.warning("bestbuy_ca: query %r failed: %s", query, e)
        return listings

    def _search_one(
        self,
        client,
        query: str,
        seen_skus: set[str],
        detail_cache: dict[str, dict | None],
    ) -> list[Listing]:
        out: list[Listing] = []
        page = 1
        while page <= MAX_PAGES_PER_QUERY:
            params = {
                "categoryid": LAPTOPS_CATEGORY,
                "query": query,
                "page": page,
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
                break

            try:
                data = resp.json()
            except Exception as e:
                logger.warning("bestbuy_ca: non-JSON response for %r: %s", query, e)
                break

            products = data.get("products", []) if isinstance(data, dict) else []
            if not isinstance(products, list):
                logger.warning("bestbuy_ca: unexpected products shape for %r", query)
                break

            for p in products:
                if not isinstance(p, dict):
                    continue
                try:
                    listing = self._parse_product(p)
                    title = " ".join((
                        str(p.get("name") or ""),
                        str(p.get("shortDescription") or ""),
                    ))
                    sku = str(p.get("sku") or "")
                    if (
                        listing is None
                        and sku
                        and _AMBIGUOUS_CPU_RE.search(title)
                        and (
                            sku in detail_cache
                            or len(detail_cache) < MAX_DETAIL_FETCHES
                        )
                    ):
                        detail = self._get_detail(client, sku, detail_cache)
                        listing = self._parse_product(p, detail)
                except RetailerBlockedError:
                    raise
                except Exception as e:
                    logger.debug("bestbuy_ca: skipping product, parse error: %s", e)
                    continue
                if listing is None or listing.sku in seen_skus:
                    continue
                seen_skus.add(listing.sku)
                out.append(listing)

            try:
                total_pages = int(data.get("totalPages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
        return out

    def _get_detail(
        self,
        client,
        sku: str,
        cache: dict[str, dict | None],
    ) -> dict | None:
        if sku in cache:
            return cache[sku]
        resp = client.get(PRODUCT_URL.format(sku=sku), params={"lang": "en-CA"})
        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(
                f"bestbuy_ca detail endpoint returned HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            logger.debug("bestbuy_ca: detail HTTP %s for SKU %s", resp.status_code, sku)
            cache[sku] = None
            return None
        try:
            detail = resp.json()
        except Exception:
            detail = None
        if not isinstance(detail, dict):
            detail = None
        cache[sku] = detail
        return detail

    def _parse_product(self, p: dict, detail: dict | None = None) -> Listing | None:
        title = str(p.get("name") or "")
        text = " ".join((title, str(p.get("shortDescription") or "")))
        if detail is not None:
            if _detail_is_sold_out(detail):
                return None
            text = f"{_detail_spec_text(detail)} {text}"
        cpu = normalize_cpu(text)
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
        condition_text = title
        if detail is not None:
            condition_text += " " + _detail_spec_text(detail)
        if (
            p.get("isOpenBox")
            or p.get("openBox")
            or re.search(r"\bopen[\s-]*box\b|\bb[\s-]*stock\b", condition_text, re.I)
        ):
            condition = "open_box"
        elif re.search(r"\brefurb", condition_text, re.I):
            condition = "refurb"

        return Listing(
            retailer=self.name,
            sku=sku,
            url=product_url,
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(text),
            gpu=_parse_gpu(text),
            price_cad=price,
            image_url=image_url,
            condition=condition,
        )
