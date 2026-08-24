"""SQLite-backed alert state, current catalog, and scheduler metadata."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from retailers.base import Listing


AlertReason = Literal["NEW", "DROP"]


@dataclass(frozen=True)
class AlertDecision:
    reason: AlertReason | None
    prev_price: Decimal | None    # last alerted price, used to compute % drop


_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    retailer              TEXT NOT NULL,
    sku                   TEXT NOT NULL,
    last_price_cad        REAL NOT NULL,
    last_alert_price_cad  REAL,
    last_seen_at          TEXT NOT NULL,
    PRIMARY KEY (retailer, sku)
);
"""

_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    retailer       TEXT NOT NULL,
    sku            TEXT NOT NULL,
    product_type   TEXT NOT NULL,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    cpu            TEXT NOT NULL,
    ram_gb         INTEGER,
    gpu            TEXT,
    price_cad      REAL NOT NULL,
    image_url      TEXT,
    condition      TEXT NOT NULL,
    retrieved_at   TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    PRIMARY KEY (retailer, sku)
);
CREATE INDEX IF NOT EXISTS idx_catalog_type_seen
    ON catalog (product_type, last_seen_at);
CREATE TABLE IF NOT EXISTS metadata (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


class Store:
    """Thin SQLite wrapper keyed by ``(retailer, sku)``."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(_SCHEMA)
        self._conn.executescript(_CATALOG_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _record_listing(self, listing: Listing, now: str) -> None:
        self._conn.execute(
            """
            INSERT INTO catalog (
                retailer, sku, product_type, url, title, cpu, ram_gb, gpu,
                price_cad, image_url, condition, retrieved_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(retailer, sku) DO UPDATE SET
                product_type = excluded.product_type,
                url = excluded.url,
                title = excluded.title,
                cpu = excluded.cpu,
                ram_gb = excluded.ram_gb,
                gpu = excluded.gpu,
                price_cad = excluded.price_cad,
                image_url = excluded.image_url,
                condition = excluded.condition,
                retrieved_at = excluded.retrieved_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                listing.retailer,
                listing.sku,
                listing.product_type,
                listing.url,
                listing.title,
                listing.cpu,
                listing.ram_gb,
                listing.gpu,
                float(listing.price_cad),
                listing.image_url,
                listing.condition,
                listing.retrieved_at.isoformat(),
                now,
            ),
        )

    def record_listing(self, listing: Listing) -> None:
        """Store the latest complete listing snapshot without alert logic."""
        now = datetime.now(timezone.utc).isoformat()
        self._record_listing(listing, now)
        self._conn.commit()

    def claim_daily_ranking(self, day: str | None = None) -> bool:
        """Return true once per UTC day and remember that day's printout."""
        day = day or datetime.now(timezone.utc).date().isoformat()
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_ranking_day'"
        ).fetchone()
        if row is not None and row[0] == day:
            return False
        self._conn.execute(
            """
            INSERT INTO metadata (key, value) VALUES ('last_ranking_day', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (day,),
        )
        self._conn.commit()
        return True

    def current_listings(
        self,
        product_type: str,
        *,
        max_age_hours: float = 36,
        max_price_cad: Decimal | None = None,
    ) -> list[Listing]:
        """Return recently seen catalog listings for rankings/Discord commands."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        sql = (
            "SELECT retailer, sku, product_type, url, title, cpu, ram_gb, gpu, "
            "price_cad, image_url, condition, retrieved_at FROM catalog "
            "WHERE product_type = ? AND last_seen_at >= ?"
        )
        params: list[object] = [product_type, cutoff_iso]
        if max_price_cad is not None:
            sql += " AND price_cad <= ?"
            params.append(float(max_price_cad))
        rows = self._conn.execute(sql, params).fetchall()

        out: list[Listing] = []
        for row in rows:
            retrieved_at = datetime.fromisoformat(row[11])
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
            out.append(Listing(
                retailer=row[0],
                sku=row[1],
                product_type=row[2],
                url=row[3],
                title=row[4],
                cpu=row[5],
                ram_gb=row[6],
                gpu=row[7],
                price_cad=Decimal(str(row[8])),
                image_url=row[9],
                condition=row[10],
                retrieved_at=retrieved_at,
            ))
        return out

    def upsert_and_diff(
        self,
        listing: Listing,
        threshold_cad: Decimal,
        drop_pct: float,
    ) -> AlertDecision:
        """Insert/update the listing, return whether to alert.

        NEW   — first time we've seen this SKU AND price <= threshold_cad.
        DROP  — known SKU; price is `drop_pct`% below the last *alerted* price.
        None  — silent.

        The row is always upserted regardless of alert outcome.
        last_alert_price_cad is updated only when we actually fire an alert.
        """
        cur = self._conn.execute(
            "SELECT last_price_cad, last_alert_price_cad FROM listings "
            "WHERE retailer = ? AND sku = ?",
            (listing.retailer, listing.sku),
        )
        row = cur.fetchone()
        price = float(listing.price_cad)
        now = datetime.now(timezone.utc).isoformat()
        self._record_listing(listing, now)

        decision = AlertDecision(reason=None, prev_price=None)

        if row is None:
            if Decimal(str(price)) <= threshold_cad:
                decision = AlertDecision(reason="NEW", prev_price=None)
            self._conn.execute(
                "INSERT INTO listings (retailer, sku, last_price_cad, "
                "last_alert_price_cad, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (
                    listing.retailer,
                    listing.sku,
                    price,
                    price if decision.reason == "NEW" else None,
                    now,
                ),
            )
        else:
            last_price, last_alert_price = row
            if last_alert_price is not None and last_alert_price > 0:
                pct_drop = (last_alert_price - price) / last_alert_price * 100
                if pct_drop >= drop_pct:
                    decision = AlertDecision(
                        reason="DROP",
                        prev_price=Decimal(str(last_alert_price)),
                    )
            elif Decimal(str(price)) <= threshold_cad:
                # Was seen before but never alerted (over-ceiling); now under.
                decision = AlertDecision(reason="NEW", prev_price=None)

            new_alert_price = (
                price if decision.reason in ("NEW", "DROP") else last_alert_price
            )
            self._conn.execute(
                "UPDATE listings SET last_price_cad = ?, last_alert_price_cad = ?, "
                "last_seen_at = ? WHERE retailer = ? AND sku = ?",
                (price, new_alert_price, now, listing.retailer, listing.sku),
            )

        self._conn.commit()
        return decision
