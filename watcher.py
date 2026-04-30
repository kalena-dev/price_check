"""laptop-price-watcher entry point.

Loads config.yaml, expands watch_tiers into a flat ceiling map, runs each
configured retailer, diffs against state.db, fires Discord embeds on
NEW/DROP, and writes back state.

Flags:
    --once               single run, then exit (default behavior anyway)
    --dry-run            print would-be alerts; no Discord, no DB writes
    --retailer NAME      restrict to one retailer (repeatable)
    --debug              print scan stats per retailer
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv

from notifier import discord as notifier
from retailers.base import (
    Listing,
    Retailer,
    RetailerBlockedError,
    RetailerParseError,
)
from store.sqlite import Store


REPO_ROOT = Path(__file__).parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"
DEFAULT_DB = REPO_ROOT / "state.db"


# Module path per retailer key; class name follows CamelCase convention.
RETAILER_REGISTRY = {
    "bestbuy_ca":      ("retailers.bestbuy_ca",      "BestBuyCA"),
    "newegg_ca":       ("retailers.newegg_ca",       "NeweggCA"),
    "canadacomputers": ("retailers.canadacomputers", "CanadaComputers"),
    "memoryexpress":   ("retailers.memoryexpress",   "MemoryExpress"),
    "apple_ca":        ("retailers.apple_ca",        "AppleCA"),
    "lenovo_ca":       ("retailers.lenovo_ca",       "LenovoCA"),
    "redflagdeals":    ("retailers.redflagdeals",    "RedFlagDeals"),
    "visions_ca":      ("retailers.visions_ca",      "VisionsCA"),
}


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def expand_watch_tiers(config: dict) -> dict[str, Decimal]:
    """Flatten watch_tiers into {canonical_cpu: max_price_cad}."""
    out: dict[str, Decimal] = {}
    tiers = config.get("watch_tiers") or {}
    for tier_name, tier in tiers.items():
        ceiling = Decimal(str(tier.get("max_price_cad", 0)))
        for cpu in tier.get("cpus", []):
            out[cpu] = ceiling
    return out


def load_retailer(key: str, config: dict) -> Retailer:
    if key not in RETAILER_REGISTRY:
        raise ValueError(f"Unknown retailer: {key}")
    mod_path, class_name = RETAILER_REGISTRY[key]
    module = importlib.import_module(mod_path)
    cls = getattr(module, class_name)
    if key == "apple_ca":
        return cls(include_refurb=config.get("apple_include_refurb", True))
    return cls()


def listing_passes_filters(
    listing: Listing,
    config: dict,
) -> bool:
    """Apply condition + RAM filters before considering for alerts."""
    conditions = set(config.get("conditions", ["new"]))
    apple_refurb_ok = config.get("apple_include_refurb", False)

    if listing.condition not in conditions:
        if not (listing.retailer == "apple_ca"
                and listing.condition == "refurb"
                and apple_refurb_ok):
            return False

    min_ram = config.get("min_ram_gb")
    if min_ram and listing.ram_gb is not None and listing.ram_gb < min_ram:
        return False

    return True


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("watcher")

    config = load_config(args.config)
    ceilings = expand_watch_tiers(config)
    if not ceilings:
        log.error("no CPUs configured in watch_tiers — aborting")
        return 2

    cpu_filter = list(ceilings.keys())
    log.info("watching %d CPUs across %d tiers", len(cpu_filter),
             len(config.get("watch_tiers", {})))

    retailer_keys = args.retailer or config.get("retailers", [])
    if not retailer_keys:
        log.error("no retailers configured — aborting")
        return 2

    drop_pct = float(config.get("alert_on_drop_pct", 5))

    load_dotenv()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook_url and not args.dry_run:
        log.warning("DISCORD_WEBHOOK_URL not set; alerts will be skipped")

    if args.dry_run:
        embeds_to_post: list[dict] = []
        store = None
    else:
        store = Store(args.db)
        embeds_to_post = []

    for key in retailer_keys:
        try:
            retailer = load_retailer(key, config)
        except Exception as e:
            log.error("failed to load retailer %r: %s", key, e)
            continue

        log.info("running %s ...", key)
        stats = Counter()
        try:
            listings = retailer.search(cpu_filter)
        except RetailerBlockedError as e:
            log.warning("%s blocked: %s — skipping for this run", key, e)
            continue
        except RetailerParseError as e:
            log.error("%s parse error: %s", key, e)
            if webhook_url and not args.dry_run:
                notifier.post_error(webhook_url, key, str(e))
            continue
        except Exception as e:
            log.error("%s unexpected error: %s", key, e)
            continue

        stats["scanned"] = len(listings)

        for listing in listings:
            if listing.cpu not in ceilings:
                continue
            stats["matched_cpu"] += 1

            if not listing_passes_filters(listing, config):
                continue
            stats["passed_filters"] += 1

            ceiling = ceilings[listing.cpu]
            if listing.price_cad <= ceiling:
                stats["under_ceiling"] += 1

            if args.dry_run:
                if listing.price_cad <= ceiling:
                    stats["would_alert_new"] += 1
                    log.info(
                        "[DRY] NEW %s %s — %s @ $%s (ceiling $%s)",
                        listing.retailer, listing.cpu, listing.title[:80],
                        listing.price_cad, ceiling,
                    )
                continue

            decision = store.upsert_and_diff(listing, ceiling, drop_pct)
            if decision.reason is None:
                continue
            stats[f"alert_{decision.reason.lower()}"] += 1
            log.info(
                "%s %s %s — $%s",
                decision.reason, listing.retailer, listing.cpu, listing.price_cad,
            )
            if webhook_url:
                embeds_to_post.append(
                    notifier.build_embed(listing, decision.reason, decision.prev_price)
                )

        log.info("%s stats: %s", key, dict(stats))

    if not args.dry_run and webhook_url and embeds_to_post:
        notifier.post(webhook_url, embeds_to_post)
        log.info("posted %d embeds to Discord", len(embeds_to_post))

    if store is not None:
        store.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="laptop-price-watcher")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to config.yaml")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="Path to state.db")
    parser.add_argument("--once", action="store_true",
                        help="Single run (default behavior)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print would-be alerts; no Discord, no DB writes")
    parser.add_argument("--retailer", action="append", default=[],
                        help="Restrict to one retailer (repeatable)")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging + per-retailer scan stats")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
