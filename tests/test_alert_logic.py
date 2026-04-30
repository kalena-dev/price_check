"""Tests for store/sqlite.py — NEW / DROP / silent decision logic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from retailers.base import Listing
from store.sqlite import Store


def _listing(price: float, sku: str = "ABC123", retailer: str = "newegg_ca") -> Listing:
    return Listing(
        retailer=retailer,
        sku=sku,
        url="https://example.com/p/" + sku,
        title="Some laptop with Ryzen 9 8945HX",
        cpu="8945HX",
        ram_gb=32,
        gpu="RTX 4080",
        price_cad=Decimal(str(price)),
        image_url=None,
    )


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    s = Store(db)
    yield s
    s.close()


def test_new_listing_under_ceiling_fires_new(store: Store) -> None:
    decision = store.upsert_and_diff(_listing(1799), threshold_cad=Decimal("1900"), drop_pct=5)
    assert decision.reason == "NEW"
    assert decision.prev_price is None


def test_new_listing_over_ceiling_silent(store: Store) -> None:
    decision = store.upsert_and_diff(_listing(2099), threshold_cad=Decimal("1900"), drop_pct=5)
    assert decision.reason is None


def test_repeated_known_listing_no_drop_silent(store: Store) -> None:
    store.upsert_and_diff(_listing(1799), threshold_cad=Decimal("1900"), drop_pct=5)
    decision = store.upsert_and_diff(_listing(1799), threshold_cad=Decimal("1900"), drop_pct=5)
    assert decision.reason is None


def test_known_listing_5pct_drop_fires_drop(store: Store) -> None:
    store.upsert_and_diff(_listing(2000), threshold_cad=Decimal("1900"), drop_pct=5)
    # Initial price was over ceiling so no NEW alert; last_alert_price was never set.
    # Bring it under ceiling — that should fire NEW (transition under).
    decision = store.upsert_and_diff(_listing(1899), threshold_cad=Decimal("1900"), drop_pct=5)
    assert decision.reason == "NEW"
    # Now drop 6% from the alert price (1899) -> 1785.
    decision2 = store.upsert_and_diff(_listing(1785), threshold_cad=Decimal("1900"), drop_pct=5)
    assert decision2.reason == "DROP"
    assert decision2.prev_price == Decimal("1899.0")


def test_small_wiggle_below_threshold_silent(store: Store) -> None:
    store.upsert_and_diff(_listing(1800), threshold_cad=Decimal("1900"), drop_pct=5)
    # 4% drop — below the 5% threshold.
    decision = store.upsert_and_diff(_listing(1730), threshold_cad=Decimal("1900"), drop_pct=5)
    assert decision.reason is None


def test_drop_measured_against_last_alerted_not_last_seen(store: Store) -> None:
    """Wiggling slightly down then back up shouldn't fire on every sub-5% drift."""
    store.upsert_and_diff(_listing(1800), threshold_cad=Decimal("1900"), drop_pct=5)  # NEW
    store.upsert_and_diff(_listing(1780), threshold_cad=Decimal("1900"), drop_pct=5)  # silent (1.1%)
    store.upsert_and_diff(_listing(1770), threshold_cad=Decimal("1900"), drop_pct=5)  # silent (vs 1800)
    decision = store.upsert_and_diff(_listing(1700), threshold_cad=Decimal("1900"), drop_pct=5)
    # 1700 vs last alerted 1800 -> 5.55% drop -> DROP fires.
    assert decision.reason == "DROP"
    assert decision.prev_price == Decimal("1800.0")


def test_separate_skus_tracked_independently(store: Store) -> None:
    a = store.upsert_and_diff(_listing(1799, sku="A"), threshold_cad=Decimal("1900"), drop_pct=5)
    b = store.upsert_and_diff(_listing(2099, sku="B"), threshold_cad=Decimal("1900"), drop_pct=5)
    assert a.reason == "NEW"
    assert b.reason is None


def test_separate_retailers_tracked_independently(store: Store) -> None:
    a = store.upsert_and_diff(
        _listing(1799, sku="X", retailer="newegg_ca"),
        threshold_cad=Decimal("1900"), drop_pct=5,
    )
    b = store.upsert_and_diff(
        _listing(1799, sku="X", retailer="bestbuy_ca"),
        threshold_cad=Decimal("1900"), drop_pct=5,
    )
    # Same SKU at different retailers should both fire NEW.
    assert a.reason == "NEW"
    assert b.reason == "NEW"
