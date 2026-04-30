"""Discord webhook notifier — rich embed builder + poster."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from dotenv import load_dotenv

from retailers.base import Listing

logger = logging.getLogger(__name__)


COLOR_NEW = 0x57F287    # green
COLOR_DROP = 0xFEE75C   # yellow/orange
COLOR_ERROR = 0xED4245  # red, used for "scraper broken" notices

# Display names per retailer key.
RETAILER_DISPLAY = {
    "bestbuy_ca": "Best Buy CA",
    "newegg_ca": "Newegg CA",
    "canadacomputers": "Canada Computers",
    "memoryexpress": "Memory Express",
    "apple_ca": "Apple CA",
}


def _format_price(p: Decimal) -> str:
    return f"${p:,.2f} CAD"


def build_embed(
    listing: Listing,
    reason: str,
    prev_price: Decimal | None,
) -> dict:
    """Build a Discord embed dict for a NEW or DROP alert."""
    color = COLOR_NEW if reason == "NEW" else COLOR_DROP
    retailer_name = RETAILER_DISPLAY.get(listing.retailer, listing.retailer)

    title = f"{listing.cpu} — {_format_price(listing.price_cad)}"
    if reason == "DROP" and prev_price is not None and prev_price > 0:
        pct = (prev_price - listing.price_cad) / prev_price * 100
        title = f"{listing.cpu} — {_format_price(listing.price_cad)} (↓{pct:.1f}%)"

    fields = [
        {"name": "Retailer", "value": retailer_name, "inline": True},
    ]
    if listing.ram_gb:
        fields.append({"name": "RAM", "value": f"{listing.ram_gb} GB", "inline": True})
    if listing.gpu:
        fields.append({"name": "GPU", "value": listing.gpu, "inline": True})
    if reason == "DROP" and prev_price is not None:
        fields.append({
            "name": "Was",
            "value": _format_price(prev_price),
            "inline": True,
        })
    if listing.condition and listing.condition != "new":
        fields.append({
            "name": "Condition",
            "value": listing.condition.replace("_", " ").title(),
            "inline": True,
        })

    embed = {
        "title": title,
        "description": listing.title[:300],
        "url": listing.url,
        "color": color,
        "fields": fields,
        "timestamp": listing.retrieved_at.isoformat(),
        "footer": {"text": f"{reason} · laptop-price-watcher"},
    }
    if listing.image_url:
        embed["thumbnail"] = {"url": listing.image_url}
    return embed


def post(webhook_url: str, embeds: list[dict]) -> None:
    """POST one or more embeds to a Discord webhook."""
    if not embeds:
        return
    # Discord accepts up to 10 embeds per request.
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        payload = {"embeds": chunk}
        try:
            resp = httpx.post(webhook_url, json=payload, timeout=15.0)
        except Exception as e:
            logger.error("discord post failed: %s", e)
            return
        if resp.status_code >= 400:
            logger.error(
                "discord webhook returned HTTP %s: %s",
                resp.status_code,
                resp.text[:300],
            )


def build_summary_embed(
    retailer: str,
    shown_count: int,
    suppressed_count: int,
) -> dict:
    """Build a summary embed when we suppressed alerts to avoid flooding."""
    retailer_name = RETAILER_DISPLAY.get(retailer, retailer)
    total = shown_count + suppressed_count
    return {
        "title": f"{retailer_name}: {total} matches under ceiling",
        "description": (
            f"Showing first **{shown_count}** alerts above; **{suppressed_count}** "
            "more matches are stored in state.db but not posted to keep the "
            "channel readable. Tighten the tier ceiling for the noisy CPU "
            "or set `max_alerts_per_retailer` in `config.yaml`."
        ),
        "color": COLOR_ERROR,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"summary - {retailer}"},
    }


def post_error(webhook_url: str, retailer: str, message: str) -> None:
    """Post a single 'scraper broken' notice."""
    embed = {
        "title": f"⚠ {retailer} scraper issue",
        "description": message[:1500],
        "color": COLOR_ERROR,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "laptop-price-watcher"},
    }
    post(webhook_url, [embed])


# --------------------------------------------------------------------------
# CLI: `python -m notifier.discord --test` smoke-tests the webhook.

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Discord notifier smoke test")
    parser.add_argument("--test", action="store_true",
                        help="Post a hardcoded test embed to the configured webhook")
    args = parser.parse_args()

    load_dotenv()
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL not set in .env", file=sys.stderr)
        return 1

    if args.test:
        sample = Listing(
            retailer="newegg_ca",
            sku="TEST-001",
            url="https://www.newegg.ca/",
            title="TEST — Sample gaming laptop with Ryzen 9 8945HX, 32GB DDR5, RTX 4080",
            cpu="8945HX",
            ram_gb=32,
            gpu="RTX 4080",
            price_cad=Decimal("1799.00"),
            image_url=None,
            condition="new",
        )
        embed = build_embed(sample, "NEW", None)
        post(url, [embed])
        print("Posted test embed. Check your Discord channel.")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
