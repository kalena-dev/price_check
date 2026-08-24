"""Tests for component parsing, catalog snapshots, and value rankings."""

from __future__ import annotations

from decimal import Decimal

from notifier.discord import build_ranking_embeds
from ranking import estimate_fair_value, rank_listings
from retailers._components import (
    looks_like_prebuilt,
    normalize_desktop_cpu,
    normalize_gpu,
    parse_system_ram,
)
from retailers.base import Listing
from retailers.bestbuy_prebuilts import BestBuyPrebuilts
from retailers.walmart_prebuilts import WalmartPrebuilts
from store.sqlite import Store


def _listing(
    sku: str,
    price: str,
    *,
    product_type: str = "prebuilt",
    condition: str = "new",
) -> Listing:
    return Listing(
        retailer="walmart_prebuilts" if product_type == "prebuilt" else "walmart_ca",
        sku=sku,
        url=f"https://example.com/{sku}",
        title=(
            "Gaming PC Desktop AMD Ryzen 7 7700X, RTX 5070, "
            "32GB DDR5 RAM, 1TB SSD"
        ),
        cpu="7700X" if product_type == "prebuilt" else "8845HS",
        ram_gb=32,
        gpu="RTX 5070" if product_type == "prebuilt" else "RTX 4060",
        price_cad=Decimal(price),
        image_url=None,
        condition=condition,
        product_type=product_type,
    )


def test_component_parser_avoids_gpu_vram_for_system_ram() -> None:
    text = (
        "Gaming PC Intel Core i5-14400F, GeForce RTX 5070 12GB, "
        "32GB DDR5 RGB RAM, 1TB SSD"
    )
    assert normalize_desktop_cpu(text) == "14400F"
    assert normalize_gpu(text) == "RTX 5070"
    assert parse_system_ram(text) == 32
    assert parse_system_ram("Intel Ci5-14400, 16GBx2 DDR5") == 32
    assert normalize_desktop_cpu("Intel Ci5-14400 gaming PC") == "14400"
    assert looks_like_prebuilt(text)
    assert not looks_like_prebuilt("ASUS RTX 5070 desktop graphics card")


def test_amd_desktop_and_radeon_normalization() -> None:
    text = "Gaming Desktop AMD Ryzen 7 9800X3D with Radeon RX 9070 XT"
    assert normalize_desktop_cpu(text) == "9800X3D"
    assert normalize_gpu(text) == "RX 9070 XT"


def test_prebuilt_adapters_build_rankable_listings() -> None:
    bestbuy_product = {
        "sku": "BB-PC",
        "name": (
            "Gaming Desktop Computer AMD Ryzen 7 7700X, "
            "RTX 5070 12GB, 32GB DDR5 RAM, 1TB SSD"
        ),
        "salePrice": 2099.99,
        "productUrl": "/en-ca/product/example/BB-PC",
    }
    walmart_product = {
        "usItemId": "WM-PC",
        "name": (
            "Prebuilt Gaming PC AMD Ryzen 7 9800X3D, Radeon RX 9070 XT, "
            "32GB DDR5 RAM, 2TB SSD"
        ),
        "price": 2999.99,
        "canonicalUrl": "/en/ip/example/WM-PC",
        "availabilityStatusDisplayValue": "In stock",
    }

    bestbuy = BestBuyPrebuilts()._parse_product(bestbuy_product)
    walmart = WalmartPrebuilts()._parse_product(walmart_product)

    assert bestbuy is not None and bestbuy.product_type == "prebuilt"
    assert bestbuy.ram_gb == 32 and bestbuy.gpu == "RTX 5070"
    assert walmart is not None and walmart.cpu == "9800X3D"
    assert walmart.gpu == "RX 9070 XT"


def test_ranking_is_best_first_and_enforces_3000_cap() -> None:
    cheap = _listing("cheap", "1800")
    expensive = _listing("expensive", "2400")
    over_cap = _listing("over", "3000.01")

    ranked = rank_listings(
        [expensive, over_cap, cheap],
        product_type="prebuilt",
        max_price_cad=Decimal("3000"),
        limit=10,
    )

    assert [deal.listing.sku for deal in ranked] == ["cheap", "expensive"]
    assert all(deal.listing.price_cad <= Decimal("3000") for deal in ranked)


def test_condition_reduces_estimated_fair_value() -> None:
    new_fair, _, _ = estimate_fair_value(_listing("new", "2000"))
    used_fair, _, _ = estimate_fair_value(
        _listing("used", "2000", condition="used")
    )
    assert used_fair < new_fair


def test_discord_rankings_emit_worst_first_best_last() -> None:
    ranked = rank_listings(
        [_listing("best", "1700"), _listing("worse", "2300")],
        product_type="prebuilt",
        limit=10,
    )
    embeds = build_ranking_embeds(ranked, "prebuilt")

    assert embeds[0]["title"].startswith("#2")
    assert embeds[-1]["title"].startswith("#1")


def test_store_round_trips_current_catalog_listing(tmp_path) -> None:
    db = tmp_path / "catalog.db"
    listing = _listing("PC1", "1999.99")
    with Store(db) as store:
        store.record_listing(listing)
        current = store.current_listings(
            "prebuilt",
            max_age_hours=1,
            max_price_cad=Decimal("3000"),
        )
        assert store.claim_daily_ranking("2026-08-23")
        assert not store.claim_daily_ranking("2026-08-23")
        assert store.claim_daily_ranking("2026-08-24")

    assert len(current) == 1
    assert current[0].sku == "PC1"
    assert current[0].product_type == "prebuilt"
    assert current[0].price_cad == Decimal("1999.99")
