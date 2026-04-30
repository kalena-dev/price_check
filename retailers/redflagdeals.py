"""RedFlagDeals.com aggregator adapter.

RFD is Canada's main deal-tracking forum. Users post deals across all
retailers — BB CA, Costco, Newegg, Walmart, etc. — in the Hot Deals forum
(forum_id=9). We search by canonical CPU strings; matching posts have
titles like '[Retailer] Lenovo Legion ... 8945HX ... $1,939'.

Caveats:
- Coverage is opportunistic — only catches deals users have noticed and posted.
- Listings are forum threads, not real product pages. Each thread has one
  representative price extracted from the title; if it changes mid-thread
  we won't notice without re-parsing the body (out of scope for v1).
- We dedupe by thread ID so the same deal doesn't fire twice.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from decimal import Decimal

from bs4 import BeautifulSoup

from retailers._http import make_client
from retailers._normalize import normalize_cpu
from retailers.base import Listing, Retailer, RetailerBlockedError

logger = logging.getLogger(__name__)


SEARCH_URL = "https://forums.redflagdeals.com/search.php"
HOT_DEALS_FORUM_ID = "9"
THREAD_BASE = "https://forums.redflagdeals.com/"


# Title format: "[Retailer] Optional Re: Product description $price"
_RETAILER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$([0-9][0-9,]*\.?\d{0,2})")
_THREAD_ID_RE = re.compile(r"-(\d{6,})/")


def _parse_decimal(text: str) -> Decimal | None:
    if not text:
        return None
    text = text.replace(",", "")
    try:
        d = Decimal(text)
    except Exception:
        return None
    return d if d > 0 else None


def _extract_retailer(title: str) -> tuple[str, str]:
    """Return (retailer_name, title_without_prefix). Empty retailer means none."""
    m = _RETAILER_RE.match(title)
    if not m:
        return "", title
    return m.group(1).strip(), title[m.end():]


def _extract_price(title: str) -> Decimal | None:
    """Pick the most likely SALE price out of an RFD post title.

    RFD titles vary wildly:
        "[Newegg] Lenovo Legion $1939"         -> $1939
        "Was $2685 Now $2290"                   -> $2290
        "$1999 Acer Predator (Save $300)"       -> $1999
        "Save $600 on Legion ($2299)"           -> $2299  (NOT $600)
        "Lenovo Legion $300 off, now $1699"     -> $1699  (NOT $300)

    Strategy: drop any $ amounts that are preceded within ~12 chars by
    'save', 'off', or 'discount' (common RFD savings phrasing), then take
    the last remaining amount above the $700 sanity floor — RFD posts
    almost always cite the final sale price last.
    """
    if not title:
        return None
    # Find all $ amounts with their string positions.
    candidates: list[tuple[int, Decimal]] = []
    for m in _PRICE_RE.finditer(title):
        d = _parse_decimal(m.group(1))
        if d is None or d < Decimal("700"):
            continue
        # Look back ~12 chars for 'save'/'off'/'discount' which signal
        # this $ amount is a savings number, not the sale price.
        prefix = title[max(0, m.start() - 16):m.start()].lower()
        if any(kw in prefix for kw in ("save", " off", "discount", "rebate")):
            continue
        candidates.append((m.start(), d))
    if not candidates:
        return None
    # Sort by position, take the last — RFD posters typically write the
    # sale price last ("was X now Y" pattern).
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def _extract_thread_id(href: str) -> str | None:
    m = _THREAD_ID_RE.search(href)
    return m.group(1) if m else None


class RedFlagDeals(Retailer):
    name = "redflagdeals"

    def search(self, cpu_filter: list[str]) -> list[Listing]:
        listings: list[Listing] = []
        seen_threads: set[str] = set()
        # Search RFD for each canonical CPU. RFD's full-text search expects
        # plain keywords — we pass them as-is.
        with make_client() as client:
            for i, query in enumerate(cpu_filter):
                if i > 0:
                    time.sleep(0.5)
                try:
                    chunk = self._search_one(client, query, seen_threads)
                except RetailerBlockedError:
                    raise
                except Exception as e:
                    logger.warning("redflagdeals: query %r failed: %s", query, e)
                    continue
                listings.extend(chunk)
        return listings

    def _search_one(
        self,
        client,
        query: str,
        seen_threads: set[str],
    ) -> list[Listing]:
        params = {
            "keywords": query,
            "forum_id": HOT_DEALS_FORUM_ID,
            "sort_by_joined_date": "lastpostdate",
            "search_orderby": "lastpostdate",
        }
        url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
        resp = client.get(url)
        if resp.status_code in (403, 429, 503):
            raise RetailerBlockedError(f"redflagdeals returned {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("redflagdeals: HTTP %s for %r", resp.status_code, query)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        out: list[Listing] = []
        for subj in soup.select(".post_subject"):
            try:
                listing = self._parse_post(subj)
            except Exception as e:
                logger.debug("redflagdeals: post parse error: %s", e)
                continue
            if listing is None:
                continue
            if listing.sku in seen_threads:
                continue
            seen_threads.add(listing.sku)
            out.append(listing)
        return out

    def _parse_post(self, subj_el) -> Listing | None:
        link = subj_el.select_one("a.post_subject_link") or subj_el.select_one("a")
        if link is None:
            return None
        title = link.get_text(strip=True)
        href = link.get("href", "")
        # Skip thread replies — only original-post threads have meaningful prices.
        # "Re: ..." indicates we're looking at a reply within a matching thread.
        # Strip the prefix but keep the title for normalization.
        title_clean = re.sub(r"\s*Re:\s*", " ", title, flags=re.IGNORECASE).strip()
        retailer_tag, title_no_prefix = _extract_retailer(title_clean)

        cpu = normalize_cpu(title_no_prefix)
        if cpu is None:
            return None

        price = _extract_price(title_clean)
        if price is None:
            return None

        thread_id = _extract_thread_id(href)
        if not thread_id:
            return None

        url = href if href.startswith("http") else THREAD_BASE + href.lstrip("/")

        # We tag each listing with the original retailer in the title so the
        # Discord embed says "RFD via Newegg" or similar. Use the [bracket]
        # prefix as a hint — kept in the title field for visibility.
        descriptive_title = title_clean
        if retailer_tag:
            descriptive_title = f"[{retailer_tag}] {title_no_prefix}".strip()

        return Listing(
            retailer=self.name,
            sku=thread_id,
            url=url,
            title=descriptive_title,
            cpu=cpu,
            ram_gb=None,
            gpu=None,
            price_cad=price,
            image_url=None,
            condition="new",
        )
