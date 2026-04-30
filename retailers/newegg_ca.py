"""Newegg CA adapter.

Strategy: Newegg's product list page embeds the full result set as JSON in
`window.__initialState__ = {...}` script. We extract that block and walk
`Products[].ItemCell` for each listing — much more stable than DOM scraping.

Note: Newegg's `Description` field is a Python-repr-style string (single
quotes, backslash-escaped) rather than nested JSON, so we pull `Title` from
it via regex rather than ast.literal_eval (which can fail on edge cases).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from decimal import Decimal

from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.newegg.ca/p/pl"
INIT_STATE_PREFIX = "window.__initialState__ = "

# Subcategory IDs we accept — gaming laptops + general laptops + Apple.
# 3365 = gaming laptops, 3326 = laptops/notebooks, 3408 = Apple laptops, etc.
# We don't filter strictly by subcategory because the CPU normalizer is
# already a tight filter. Keep this list informational only.

def _extract_init_state(html: str) -> dict | None:
    idx = html.find(INIT_STATE_PREFIX)
    if idx < 0:
        return None
    end = html.find("</script>", idx)
    if end < 0:
        return None
    raw = html[idx + len(INIT_STATE_PREFIX):end].strip().rstrip(";").strip()
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("newegg_ca: failed to parse __initialState__: %s", e)
        return None


def _extract_title(description) -> str | None:
    """Description may be a dict (parsed) or a string (legacy). Pull Title."""
    if not description:
        return None
    if isinstance(description, dict):
        title = description.get("Title")
        return title if isinstance(title, str) and title else None
    if isinstance(description, str):
        m = re.search(r"'Title'\s*:\s*'((?:\\'|[^'])*)'", description)
        if m:
            return m.group(1).replace("\\'", "'")
    return None


def _build_image_url(item_number: str, image_name_list: str) -> str | None:
    if not image_name_list:
        return None
    first = image_name_list.split(",", 1)[0].strip()
    if not first:
        return None
    # Newegg's CDN serves product thumbnails at this prefix.
    return f"https://c1.neweggimages.com/productimage/nb1280/{first}"


def _build_product_url(item_number: str) -> str:
    """Construct the canonical product page URL from Newegg's Item identifier.

    Catalog items use a hyphenated ID like "34-840-623" -> N82E1634840623.
    Marketplace items use a flat SKU like "9SIARXSKNV4969" -> direct.
    """
    if "-" in item_number:
        flat = item_number.replace("-", "")
        return f"https://www.newegg.ca/p/N82E16{flat}"
    return f"https://www.newegg.ca/p/{item_number}"


def _parse_ram(title: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*GB\s*(?:DDR|RAM|Memory)", title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,3})\s*GB\b", title, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_gpu(title: str) -> str | None:
    m = re.search(
        r"\b(RTX\s?\d{4}\s?(?:Ti|Super)?|Radeon\s+RX\s+\d{4}\s?M?|GTX\s?\d{4})\b",
        title,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


class NeweggCA(Retailer):
    name = "newegg_ca"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        listings: list[Listing] = []
        seen_skus: set[str] = set()
        consecutive_400 = 0
        with make_client() as client:
            for i, query in enumerate(cpu_filter):
                if i > 0:
                    time.sleep(0.4)  # politeness delay; avoid rate-limit triggers
                try:
                    chunk, status = self._search_one_with_status(client, query, seen_skus)
                except RetailerBlockedError:
                    raise
                except Exception as e:
                    logger.warning("newegg_ca: query %r failed: %s", query, e)
                    continue
                listings.extend(chunk)
                # Bail early if Newegg is uniformly returning 400 — it's
                # blocking our IP rather than legitimately having no results.
                if status == 400:
                    consecutive_400 += 1
                    if consecutive_400 >= 5:
                        raise RetailerBlockedError(
                            "newegg_ca returning 400 across 5+ queries — "
                            "IP appears rate-limited"
                        )
                else:
                    consecutive_400 = 0
        return listings

    def _search_one_with_status(
        self,
        client,
        query: str,
        seen_skus: set[str],
    ) -> tuple[list[Listing], int]:
        params = {"d": query}
        url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
        resp = client.get(url)
        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(f"newegg_ca returned {resp.status_code}")
        if resp.status_code == 400:
            # Either a very short / unrecognized query OR a rate-limit error
            # page (Newegg uses HTTP 400 for both). Skip; the caller's
            # consecutive-400 counter will escalate if every query 400s.
            logger.debug("newegg_ca: HTTP 400 for %r", query)
            return [], 400
        if resp.status_code != 200:
            logger.warning("newegg_ca: HTTP %s for %r", resp.status_code, query)
            return [], resp.status_code

        state = _extract_init_state(resp.text)
        if state is None:
            return [], resp.status_code

        products = state.get("Products") or []
        if not isinstance(products, list):
            return [], resp.status_code

        out: list[Listing] = []
        for p in products:
            try:
                listing = self._parse_product(p)
            except Exception as e:
                logger.debug("newegg_ca: skipping product, parse error: %s", e)
                continue
            if listing is None:
                continue
            if listing.sku in seen_skus:
                continue
            seen_skus.add(listing.sku)
            out.append(listing)
        return out, resp.status_code

    def _parse_product(self, product: dict) -> Listing | None:
        cell = product.get("ItemCell")
        if not isinstance(cell, dict):
            return None
        item = str(cell.get("Item") or "")
        if not item:
            return None

        # Drop non-laptop SKUs early (mini PCs, CPUs, accessories all match
        # CPU regex but aren't what we're tracking).
        sub = cell.get("Subcategory") or {}
        sub_desc = ""
        if isinstance(sub, dict):
            sub_desc = (
                sub.get("SubcategoryDescription")
                or sub.get("RealSubCategoryDescription")
                or ""
            )
        if "laptop" not in sub_desc.lower() and "notebook" not in sub_desc.lower():
            return None

        title = _extract_title(cell.get("Description"))
        if not title:
            return None

        cpu = normalize_cpu(title)
        if cpu is None:
            return None

        # Prefer UnitCost (sticker) over FinalPrice (often includes shipping/fees
        # that distort price comparisons).
        price_value = cell.get("UnitCost")
        if price_value is None:
            price_value = cell.get("FinalPrice")
        if price_value is None:
            return None
        try:
            price = Decimal(str(price_value))
        except Exception:
            return None
        if price <= 0:
            return None

        image_info = cell.get("Image") or {}
        normal = image_info.get("Normal") if isinstance(image_info, dict) else None
        image_url = None
        if isinstance(normal, dict):
            image_url = _build_image_url(item, normal.get("ImageNameList") or "")

        condition = "new"
        ti = title.lower()
        if "open box" in ti or "open-box" in ti:
            condition = "open_box"
        if "refurb" in ti:
            condition = "refurb"

        return Listing(
            retailer=self.name,
            sku=item,
            url=_build_product_url(item),
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(title),
            gpu=_parse_gpu(title),
            price_cad=price,
            image_url=image_url,
            condition=condition,
        )
