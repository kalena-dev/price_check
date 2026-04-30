"""SQLite-backed state store for the watcher.

Tracks (retailer, sku) -> last seen price + last alerted price so we can
distinguish NEW listings, real DROPs, and noise.
"""

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


class Store:
    """Thin SQLite wrapper. One row per (retailer, sku)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_) -> None:
        self.close()

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
