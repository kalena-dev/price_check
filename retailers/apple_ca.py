"""Apple CA adapter — MacBook Pro new + refurb.

Apple's product cards are server-rendered. Each card is a `<li>` (refurb) or
similar that contains a heading with the product name and a
`.as-producttile-currentprice` div with the price.

  New:    https://www.apple.com/ca/shop/buy-mac/macbook-pro
  Refurb: https://www.apple.com/ca/shop/refurbished/mac/macbook-pro

We anchor on the `.as-producttile-currentprice` element and walk up the DOM
to find the nearest ancestor that contains a heading element — that's the
tile root. From there we pull title, link, image, and SKU.
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


URL_NEW = "https://www.apple.com/ca/shop/buy-mac/macbook-pro"
URL_REFURB = "https://www.apple.com/ca/shop/refurbished/mac/macbook-pro"

_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{1,2})?)")
_SKU_FROM_URL_RE = re.compile(r"/shop/product/([A-Z0-9]+/[A-Z0-9]+)", re.IGNORECASE)


def _parse_price(text: str | None) -> Decimal | None:
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    cleaned = m.group(1).replace(",", "")
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _parse_ram(text: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*GB\b", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


class AppleCA(Retailer):
    name = "apple_ca"

    def __init__(self, include_refurb: bool = True):
        self.include_refurb = include_refurb

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        listings: list[Listing] = []
        seen_skus: set[str] = set()
        with make_client() as client:
            try:
                listings.extend(self._scrape(client, URL_NEW, "new", seen_skus))
            except RetailerBlockedError:
                raise
            except Exception as e:
                logger.warning("apple_ca: new-store scrape failed: %s", e)
            if self.include_refurb:
                try:
                    listings.extend(
                        self._scrape(client, URL_REFURB, "refurb", seen_skus)
                    )
                except RetailerBlockedError:
                    raise
                except Exception as e:
                    logger.warning("apple_ca: refurb-store scrape failed: %s", e)
        return listings

    def _scrape(
        self,
        client,
        url: str,
        condition: str,
        seen_skus: set[str],
    ) -> list[Listing]:
        resp = client.get(url)
        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(f"apple_ca returned HTTP {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("apple_ca: HTTP %s for %s", resp.status_code, url)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        out: list[Listing] = []
        for price_el in soup.select(".as-producttile-currentprice"):
            try:
                listing = self._parse_tile(price_el, url, condition)
            except Exception as e:
                logger.debug("apple_ca: tile parse error: %s", e)
                continue
            if listing is None:
                continue
            if listing.sku in seen_skus:
                continue
            seen_skus.add(listing.sku)
            out.append(listing)
        return out

    def _parse_tile(self, price_el, page_url: str, condition: str) -> Listing | None:
        # Walk up to nearest ancestor that contains a heading element.
        cur = price_el
        for _ in range(10):
            cur = cur.parent
            if cur is None:
                return None
            heading = cur.find(["h2", "h3", "h4"]) if hasattr(cur, "find") else None
            if heading is not None:
                tile = cur
                break
        else:
            return None

        title = heading.get_text(strip=True)
        if not title:
            return None
        cpu = normalize_cpu(title)
        if cpu is None:
            return None

        price = _parse_price(price_el.get_text(" ", strip=True))
        if price is None or price <= 0:
            return None

        link = tile.find("a", href=True)
        href = link.get("href") if link else ""
        if href.startswith("/"):
            href = "https://www.apple.com" + href
        if not href:
            href = page_url

        sku_match = _SKU_FROM_URL_RE.search(href)
        sku = sku_match.group(1).lower() if sku_match else href.rsplit("?", 1)[0]
        if not sku:
            return None

        img_el = tile.find("img")
        image_url = None
        if img_el is not None:
            image_url = img_el.get("src") or img_el.get("data-src")
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

        return Listing(
            retailer=self.name,
            sku=sku,
            url=href,
            title=title,
            cpu=cpu,
            ram_gb=_parse_ram(title),
            gpu=None,
            price_cad=price,
            image_url=image_url,
            condition=condition,
        )
