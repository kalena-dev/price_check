"""Stock snapshots must invalidate previous deals without firing alerts."""

from dataclasses import replace
from decimal import Decimal
import sqlite3
import pytest

from notifier.discord import build_embed, build_ranking_embeds
from ranking import rank_listings
from store.sqlite import Store
from tests.test_ranking import _listing
from watcher import listing_passes_filters


def test_stock_updates_remove_and_restore_current_deal(tmp_path):
    available = replace(_listing("PC", "1800"), availability="in_stock")
    with Store(tmp_path / "stock.db") as store:
        store.record_listing(available)
        assert store.current_listings("prebuilt")[0].availability == "in_stock"
        store.record_listing(replace(available, availability="out_of_stock"))
        assert store.current_listings("prebuilt") == []
        store.record_listing(replace(available, availability="unknown"))
        assert store.current_listings("prebuilt")[0].availability == "unknown"
        store.record_listing(available)
        assert len(store.current_listings("prebuilt")) == 1


def test_out_of_stock_does_not_create_or_consume_price_alert(tmp_path):
    item = replace(_listing("PC", "1800"), availability="out_of_stock")
    with Store(tmp_path / "stock.db") as store:
        assert store.upsert_and_diff(item, Decimal("2500"), 5).reason is None
        assert store.upsert_and_diff(replace(item, availability="in_stock"), Decimal("2500"), 5).reason == "NEW"
        cheaper = replace(item, price_cad=Decimal("1500"))
        assert store.upsert_and_diff(cheaper, Decimal("2500"), 5).reason is None
        assert store.current_listings("prebuilt") == []
        assert store.upsert_and_diff(replace(cheaper, availability="in_stock"), Decimal("2500"), 5).reason == "DROP"


def test_ranking_and_filters_exclude_known_out_of_stock():
    available = replace(_listing("yes", "1800"), availability="in_stock")
    sold_out = replace(_listing("no", "1200"), availability="out_of_stock")
    unknown = _listing("unknown", "1900")
    assert not listing_passes_filters(sold_out, {})
    ranked = rank_listings([available, sold_out, unknown], product_type="prebuilt")
    assert {deal.listing.sku for deal in ranked} == {"yes", "unknown"}
    embeds = build_ranking_embeds(ranked, "prebuilt")
    stock_labels = [field["value"] for embed in embeds for field in embed["fields"] if field["name"] == "Stock"]
    assert "In stock (last scan)" in stock_labels
    assert "Unverified — check retailer" in stock_labels
    assert any(field["value"] == "Unverified — check retailer" for field in build_embed(unknown, "NEW", None)["fields"])


def test_migrate_legacy_catalog_preserves_rows_as_unknown(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE catalog (
        retailer TEXT NOT NULL, sku TEXT NOT NULL, product_type TEXT NOT NULL,
        url TEXT NOT NULL, title TEXT NOT NULL, cpu TEXT NOT NULL,
        ram_gb INTEGER, gpu TEXT, price_cad REAL NOT NULL, image_url TEXT,
        condition TEXT NOT NULL, retrieved_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        PRIMARY KEY (retailer, sku))""")
    item = _listing("old", "1800")
    stamp = item.retrieved_at.isoformat()
    conn.execute("INSERT INTO catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (item.retailer, item.sku, item.product_type, item.url, item.title,
                  item.cpu, item.ram_gb, item.gpu, float(item.price_cad), None,
                  item.condition, stamp, stamp))
    conn.commit()
    conn.close()
    for _ in range(2):  # migration is idempotent
        with Store(path) as store:
            rows = store.current_listings("prebuilt")
            assert len(rows) == 1 and rows[0].availability == "unknown"


def test_watcher_records_sold_out_before_filters(tmp_path, monkeypatch):
    import argparse
    import watcher

    laptop = replace(_listing("LAP", "1800", product_type="laptop"), availability="in_stock")
    desktop = replace(_listing("PC", "1800"), availability="in_stock")
    db = tmp_path / "watcher.db"
    with Store(db) as store:
        store.record_listing(laptop)
        store.record_listing(desktop)

    class Retailer:
        def __init__(self, item):
            self.item = item

        def search(self, _):
            return [replace(self.item, availability="out_of_stock")]

    monkeypatch.setattr(watcher, "load_config", lambda _: {
        "watch_tiers": {"test": {}}, "retailers": ["laptops"],
        "prebuilt_retailers": ["desktops"], "ranking": {"presentation": "menu"},
    })
    monkeypatch.setattr(watcher, "expand_watch_tiers", lambda _: {laptop.cpu: Decimal("2500")})
    monkeypatch.setattr(watcher, "load_retailer", lambda key, _: Retailer(laptop if key == "laptops" else desktop))
    monkeypatch.setattr(watcher.notifier, "post", lambda *_: (_ for _ in ()).throw(AssertionError("No stock alerts or menu-mode webhook stacks")))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    args = argparse.Namespace(config="unused", db=db, retailer=None, dry_run=False, debug=False)
    assert watcher.run(args) == 0
    with Store(db) as store:
        assert store.current_listings("laptop") == []
        assert store.current_listings("prebuilt") == []


@pytest.mark.parametrize("presentation, expected_posts", [(None, 0), ("menu", 0), ("stack", 1)])
def test_watcher_only_sends_ranking_stacks_when_explicitly_enabled(tmp_path, monkeypatch, presentation, expected_posts):
    import argparse
    import watcher
    from unittest.mock import Mock

    desktop = replace(_listing("PC", "1800"), availability="in_stock")

    class Retailer:
        def search(self, _):
            return [desktop]

    ranking = {} if presentation is None else {"presentation": presentation}
    monkeypatch.setattr(watcher, "load_config", lambda _: {
        "watch_tiers": {"test": {}}, "retailers": [],
        "prebuilt_retailers": ["desktops"], "ranking": ranking,
    })
    monkeypatch.setattr(watcher, "expand_watch_tiers", lambda _: {"8845HS": Decimal("2500")})
    monkeypatch.setattr(watcher, "load_retailer", lambda *_: Retailer())
    post = Mock()
    monkeypatch.setattr(watcher.notifier, "post", post)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/webhook")
    db = tmp_path / "rankings.db"
    args = argparse.Namespace(config="unused", db=db, retailer=None, dry_run=False, debug=False)
    assert watcher.run(args) == 0
    assert post.call_count == expected_posts
    with Store(db) as store:
        assert len(store.current_listings("prebuilt")) == 1
        assert store.daily_ranking_due() == (expected_posts == 0)
