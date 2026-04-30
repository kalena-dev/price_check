"""Listing dataclass, Retailer ABC, and shared exceptions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


class RetailerError(Exception):
    """Base class for retailer-related errors."""


class RetailerBlockedError(RetailerError):
    """Retailer blocked the request (Cloudflare, Akamai, 403, etc.).

    Watcher catches this per-retailer and continues with others.
    """


class RetailerParseError(RetailerError):
    """Retailer responded but the shape changed.

    Watcher catches this and surfaces as a single rate-limited Discord notice.
    """


@dataclass(frozen=True)
class Listing:
    retailer: str
    sku: str
    url: str
    title: str
    cpu: str                       # canonical form from _normalize.normalize_cpu
    ram_gb: int | None
    gpu: str | None
    price_cad: Decimal
    image_url: str | None
    condition: str = "new"         # "new" | "open_box" | "refurb" | "used"
    retrieved_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class Retailer(ABC):
    """Adapter interface — every retailer module exposes one of these."""

    name: str = ""  # set by subclass, e.g. "newegg_ca"

    @abstractmethod
    def search(self, cpu_filter: list[str]) -> list[Listing]:
        """Return all listings the retailer has matching any CPU in cpu_filter.

        cpu_filter is a list of canonical CPU strings (e.g. ["8945HX", "275HX"]).
        Implementations may issue one query per CPU or one broad query —
        whatever's most efficient for the retailer's API.

        Listings with cpu=None (i.e. couldn't be normalized) MUST be dropped
        before returning. The watcher trusts cpu is canonical.
        """
        raise NotImplementedError
