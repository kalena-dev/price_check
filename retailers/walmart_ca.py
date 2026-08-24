"""Walmart Canada adapter.

Walmart's server-rendered search page embeds its structured result data in the
``__NEXT_DATA__`` JSON script. Reading that payload is faster and less brittle
than scraping generated CSS classes, and includes marketplace inventory as
well as products sold directly by Walmart.

Walmart's search can be fuzzy, so a small set of processor-family queries is
used instead of one request per configured CPU. Results are then restricted
to laptop categories and normalized against the caller's CPU allowlist.
"""

from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal

from bs4 import BeautifulSoup

from retailers._components import parse_system_ram
from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import (
    Listing,
    Retailer,
    RetailerBlockedError,
    RetailerParseError,
)

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.walmart.ca/en/search"
MAX_PAGES_PER_QUERY = 2
MAX_DETAIL_FETCHES = 3
MIN_LAPTOP_PRICE = Decimal("400")

# Family searches avoid dozens of large Walmart page requests while still
# covering AMD, Intel, and every configured Apple generation.
BROAD_QUERIES = (
    "Ryzen 9 HX laptop",
    "Ryzen AI laptop",
    "Core Ultra HX laptop",
    "Core i9 HX laptop",
    "MacBook Pro M3",
    "MacBook Pro M4",
    "MacBook Pro M5",
)

_NEXT_DATA_RE = re.compile(
    r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_PRICE_RE = re.compile(r"([0-9][0-9,]*(?:\.\d{1,2})?)")
# Apple result cards usually omit the CPU core count required to distinguish
# tiers. Detail enrichment is deliberately limited to these cards; following
# too many Walmart product links in one session quickly triggers rate limits.
_AMBIGUOUS_CPU_RE = re.compile(
    r"\bm[345]\s+(?:pro|max)\b",
    re.IGNORECASE,
)


def _extract_next_data(html: str) -> dict | None:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _search_result(data: dict) -> dict | None:
    try:
        result = data["props"]["pageProps"]["initialData"]["searchResult"]
    except (KeyError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def _iter_products(result: dict):
    """Yield product dictionaries from Walmart's result stacks."""
    for stack in result.get("itemStacks") or []:
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items") or []:
            if (
                isinstance(item, dict)
                and item.get("usItemId")
                and item.get("name")
            ):
                yield item


def _detail_data(data: dict) -> dict | None:
    try:
        detail = data["props"]["pageProps"]["initialData"]["data"]
    except (KeyError, TypeError):
        return None
    return detail if isinstance(detail, dict) else None


def _detail_text(detail: dict) -> str:
    idml = detail.get("idml") or {}
    highlights = idml.get("productHighlights") if isinstance(idml, dict) else []
    values: dict[str, str] = {}
    all_values: list[str] = []
    for spec in highlights or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip().lower()
        value = str(spec.get("value") or "").strip()
        if value:
            all_values.append(value)
        if name and value:
            values[name] = value

    processor = values.get("processor type", "")
    cores = values.get("processor core count", "")
    processor_with_cores = processor
    if processor and cores:
        processor_with_cores = f"{processor} {cores}-core CPU"

    descriptions: list[str] = []
    if isinstance(idml, dict):
        for key in ("shortDescription", "longDescription"):
            descriptions.append(
                BeautifulSoup(str(idml.get(key) or ""), "html.parser").get_text(
                    " ", strip=True
                )
            )
    return " ".join(
        part for part in (
            processor_with_cores,
            " ".join(all_values),
            *descriptions,
        ) if part
    )


def _parse_decimal(value) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)):
        try:
            amount = Decimal(str(value))
        except Exception:
            return None
        return amount if amount > 0 else None
    match = _PRICE_RE.search(str(value or ""))
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except Exception:
        return None
    return amount if amount > 0 else None


def _parse_ram(text: str) -> int | None:
    return parse_system_ram(text)


def _parse_gpu(text: str) -> str | None:
    match = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?|GTX\s?\d{4})\b",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _is_laptop(item: dict, title: str) -> bool:
    category = item.get("category") or {}
    path = category.get("path") if isinstance(category, dict) else []
    category_text = " ".join(
        str(entry.get("name") or "")
        for entry in (path or [])
        if isinstance(entry, dict)
    ).lower()
    if "laptop" in category_text or "notebook" in category_text:
        return True
    return bool(re.search(r"\b(?:laptop|notebook|macbook)\b", title, re.IGNORECASE))


def _condition(item: dict, title: str, detail_text: str = "") -> str:
    raw = " ".join((
        title,
        detail_text,
        str(item.get("condition") or ""),
        str(item.get("conditionV2") or ""),
    ))
    if re.search(r"\bopen[\s-]*box\b|\bb[\s-]*stock\b", raw, re.IGNORECASE):
        return "open_box"
    if re.search(r"\brefurb(?:ished)?\b|\brenewed\b", raw, re.IGNORECASE):
        return "refurb"
    if re.search(r"\bused\b|\bpre[\s-]*owned\b", raw, re.IGNORECASE):
        return "used"
    return "new"


class WalmartCA(Retailer):
    name = "walmart_ca"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        allowed = set(cpu_filter)
        listings: list[Listing] = []
        seen_skus: set[str] = set()
        detail_cache: dict[str, dict | None] = {}

        with make_client(timeout=30.0) as client:
            for index, query in enumerate(BROAD_QUERIES):
                if index:
                    time.sleep(0.4)
                for page in range(1, MAX_PAGES_PER_QUERY + 1):
                    try:
                        chunk, has_more = self._search_page(
                            client, query, page, allowed, seen_skus, detail_cache
                        )
                    except RetailerBlockedError:
                        # Walmart may rate-limit midway through a run. Preserve
                        # useful results already collected from earlier queries.
                        if listings:
                            logger.warning(
                                "walmart_ca: rate-limited after %d listings; "
                                "returning partial results",
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
        allowed: set[str],
        seen_skus: set[str],
        detail_cache: dict[str, dict | None],
    ) -> tuple[list[Listing], bool]:
        params = {"q": query}
        if page > 1:
            params["page"] = page
        resp = client.get(SEARCH_URL, params=params)

        final_url = str(getattr(resp, "url", ""))
        if resp.status_code in (403, 429, 503) or "/blocked" in final_url:
            raise RetailerBlockedError(
                f"walmart_ca returned HTTP {resp.status_code} or a block page"
            )
        if resp.status_code != 200:
            logger.warning(
                "walmart_ca: HTTP %s for %r page %d",
                resp.status_code, query, page,
            )
            return [], False

        data = _extract_next_data(resp.text)
        result = _search_result(data) if data is not None else None
        if result is None:
            raise RetailerParseError(
                "Walmart search page no longer contains expected __NEXT_DATA__ results"
            )

        out: list[Listing] = []
        for item in _iter_products(result):
            try:
                listing = self._parse_product(item, allowed)
                title = str(item.get("name") or "")
                card_text = f"{title} {item.get('shortDescription') or ''}"
                sku = str(item.get("usItemId") or "")
                if (
                    listing is None
                    and normalize_cpu(card_text) is None
                    and sku
                    and _AMBIGUOUS_CPU_RE.search(title)
                    and (sku in detail_cache or len(detail_cache) < MAX_DETAIL_FETCHES)
                ):
                    detail = self._get_detail(client, item, detail_cache)
                    listing = self._parse_product(item, allowed, detail)
            except Exception as exc:
                logger.debug("walmart_ca: product parse error: %s", exc)
                continue
            if listing is None or listing.sku in seen_skus:
                continue
            seen_skus.add(listing.sku)
            out.append(listing)

        return out, bool(result.get("hasMorePages"))

    def _get_detail(
        self,
        client,
        item: dict,
        cache: dict[str, dict | None],
    ) -> dict | None:
        sku = str(item.get("usItemId") or "")
        if sku in cache:
            return cache[sku]
        url = str(item.get("canonicalUrl") or "")
        if url.startswith("/"):
            url = "https://www.walmart.ca" + url
        if not url:
            cache[sku] = None
            return None
        try:
            time.sleep(0.5)
            resp = client.get(url)
        except Exception as exc:
            logger.debug("walmart_ca: detail request failed for %s: %s", sku, exc)
            cache[sku] = None
            return None
        final_url = str(getattr(resp, "url", ""))
        if resp.status_code != 200 or "/blocked" in final_url:
            logger.debug("walmart_ca: detail HTTP %s for %s", resp.status_code, sku)
            cache[sku] = None
            return None
        data = _extract_next_data(resp.text)
        detail = _detail_data(data) if data is not None else None
        cache[sku] = detail
        return detail

    def _parse_product(
        self,
        item: dict,
        allowed: set[str],
        detail: dict | None = None,
    ) -> Listing | None:
        title = str(item.get("name") or "").strip()
        if not title or not _is_laptop(item, title):
            return None

        description = BeautifulSoup(
            str(item.get("shortDescription") or ""), "html.parser"
        ).get_text(" ", strip=True)
        enriched_text = _detail_text(detail) if detail is not None else ""
        text = f"{enriched_text} {title} {description}"
        cpu = normalize_cpu(text)
        if cpu is None or cpu not in allowed:
            return None

        sku = str(item.get("usItemId") or "").strip()
        if not sku:
            return None

        detail_product = detail.get("product") if detail is not None else {}
        if not isinstance(detail_product, dict):
            detail_product = {}
        availability = str(
            detail_product.get("availabilityStatus")
            or item.get("availabilityStatusDisplayValue")
            or item.get("availabilityStatus")
            or ""
        ).lower().replace("_", " ")
        if any(word in availability for word in ("out of stock", "unavailable")):
            return None

        price = _parse_decimal(item.get("price"))
        if price is None:
            price_info = item.get("priceInfo") or {}
            if isinstance(price_info, dict):
                price = _parse_decimal(
                    price_info.get("linePrice")
                    or price_info.get("linePriceDisplay")
                )
        if price is None:
            detail_price = detail_product.get("priceInfo") or {}
            if isinstance(detail_price, dict):
                price = _parse_decimal(
                    detail_price.get("currentPrice")
                    or detail_price.get("linePrice")
                )
        if price is None or price < MIN_LAPTOP_PRICE:
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
            ram_gb=_parse_ram(text),
            gpu=_parse_gpu(text),
            price_cad=price,
            image_url=image_url,
            condition=_condition(item, title, enriched_text),
        )
