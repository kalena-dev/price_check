"""Transparent component-based value ranking for laptops and prebuilts.

The value index is intentionally simple and auditable:

    estimated fair hardware value / current price * 100

Fair value is a CAD component estimate (platform/chassis + CPU + optional GPU
+ RAM), adjusted for condition. It is a comparison heuristic, not a resale
appraisal or a live benchmark feed. Keeping the tables here makes changes
reviewable and prevents an opaque score from silently deciding deal order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from retailers._components import normalize_gpu
from retailers.base import Listing


# Values represent each CPU's contribution to a complete laptop in CAD.
LAPTOP_CPU_VALUES = {
    # AMD Fire/Dragon Range and mobile.
    "9955HX3D": 950, "9955HX": 850, "9850HX": 760,
    "8945HX": 650, "8940HX": 620, "8745HX": 520,
    "7945HX3D": 820, "7945HX": 720, "7845HX": 590, "7840HX": 540,
    "8945HS": 390, "8845HS": 360, "7945HS": 370,
    "7840HS": 330, "7735HS": 260, "7700G": 300,
    # Ryzen AI / Strix Halo. Max parts include unusually strong integrated GPUs.
    "AI Max+ Pro 395": 1500, "AI Max+ 395": 1450, "AI Max+ 392": 1300,
    "AI Max Pro 390": 1200, "AI Max 390": 1150,
    "AI Max Pro 385": 1000, "AI Max 385": 950,
    "HX_PRO 470": 780, "HX 470": 750, "HX_PRO 375": 680,
    "HX 375": 650, "HX_PRO 370": 620, "HX 370": 600,
    "HX 365": 500, "H 465": 440,
    # Intel Core Ultra mobile.
    "290HX": 930, "285HX": 850, "275HX": 700, "265HX": 590,
    "255HX": 510, "245HX": 440, "235HX": 390,
    "388H": 650, "358H": 540, "356H": 500, "285H": 520,
    "275H": 470, "265H": 430,
    # Intel legacy mobile.
    "14900HX": 610, "13980HX": 590, "13950HX": 560,
    "13900HX": 540, "13700HX": 430, "13700H": 350, "13620H": 300,
    # Apple SoCs include integrated-GPU value.
    "M5 Max 18": 1950, "M5 Max 14": 1650,
    "M5 Pro 18": 1550, "M5 Pro 15": 1350,
    "M4 Max 16": 1750, "M4 Max 14": 1500,
    "M4 Pro 14": 1250, "M4 Pro 12": 1080,
    "M3 Max 16": 1500, "M3 Max 14": 1300,
    "M3 Pro 12": 1000, "M3 Pro 11": 900,
}

# Desktop CPU street-value contributions in CAD.
DESKTOP_CPU_VALUES = {
    "9950X3D": 950, "9950X": 760, "9900X3D": 760, "9900X": 600,
    "9800X3D": 700, "9700X": 430, "9600X": 330,
    "7950X3D": 750, "7950X": 620, "7900X3D": 620, "7900X": 500,
    "7800X3D": 570, "7700X": 350, "7700": 320, "7600X": 270,
    "8700G": 340, "8700F": 290, "5700G": 220, "5700X": 210,
    "285K": 760, "285KF": 730, "265K": 520, "265KF": 500,
    "245K": 360, "245KF": 340,
    "14900KS": 720, "14900K": 620, "14900KF": 590, "14900F": 470,
    "14700K": 500, "14700KF": 470, "14700F": 390,
    "14600K": 360, "14600KF": 340, "14500": 280,
    "14400F": 240, "14400": 260,
    "13900K": 540, "13900KF": 510, "13700K": 410,
    "13700F": 330, "13600K": 320, "13400F": 210,
    "12900K": 390, "12900KF": 370, "12700K": 290,
}

LAPTOP_GPU_VALUES = {
    "RTX 5090": 1750, "RTX 5080": 1350, "RTX 5070 Ti": 950,
    "RTX 5070": 700, "RTX 5060 Ti": 580, "RTX 5060": 500,
    "RTX 5050": 350, "RTX 4090": 1450, "RTX 4080": 1100,
    "RTX 4070": 620, "RTX 4060": 420, "RTX 4050": 270,
    "RTX 3080 Ti": 680, "RTX 3080": 600, "RTX 3070 Ti": 450,
    "RTX 3070": 390, "RTX 3060": 280, "RTX 3050": 180,
    "RX 7900M": 850, "RX 7800M": 650, "RX 7700M": 480,
}

DESKTOP_GPU_VALUES = {
    "RTX 5090": 2800, "RTX 5080": 1650, "RTX 5070 Ti": 1050,
    "RTX 5070": 760, "RTX 5060 Ti": 600, "RTX 5060": 460,
    "RTX 5050": 350, "RTX 4090": 2200, "RTX 4080 Super": 1400,
    "RTX 4080": 1300, "RTX 4070 Ti": 900, "RTX 4070 Super": 820,
    "RTX 4070": 700, "RTX 4060 Ti": 520, "RTX 4060": 400,
    "RTX 3050": 230, "RTX 2060": 180,
    "RX 9070 XT": 1000, "RX 9070": 820, "RX 9060 XT": 560,
    "RX 7900 XT": 900, "RX 7900 GRE": 720, "RX 7800 XT": 650,
    "RX 7700 XT": 520, "RX 7600 XT": 390, "RX 7600": 320,
    "RX 550": 70,
}

CONDITION_MULTIPLIERS = {
    "new": Decimal("1.00"),
    "open_box": Decimal("0.90"),
    "refurb": Decimal("0.80"),
    "used": Decimal("0.68"),
}


@dataclass(frozen=True)
class RankedDeal:
    listing: Listing
    fair_value_cad: Decimal
    value_index: Decimal
    confidence: str


def _fallback_cpu_value(cpu: str, product_type: str) -> Decimal:
    """Conservative fallback for a normalized model missing from the table."""
    digits = re.search(r"(\d{3,5})", cpu)
    if not digits:
        return Decimal("350")
    model = digits.group(1)
    if product_type == "prebuilt":
        # Unknown modern desktop CPUs get a deliberately modest estimate.
        if model.startswith(("9", "7")):
            return Decimal("330")
        if model.startswith("14"):
            return Decimal("280")
        if model.startswith("13"):
            return Decimal("240")
        return Decimal("200")
    if "HX" in cpu:
        return Decimal("500")
    if cpu.endswith(("H", "HS")):
        return Decimal("350")
    return Decimal("300")


def cpu_value(cpu: str, product_type: str) -> tuple[Decimal, bool]:
    table = DESKTOP_CPU_VALUES if product_type == "prebuilt" else LAPTOP_CPU_VALUES
    if cpu in table:
        return Decimal(table[cpu]), True
    return _fallback_cpu_value(cpu, product_type), False


def gpu_value(gpu: str | None, product_type: str) -> tuple[Decimal, bool]:
    if not gpu:
        return Decimal("0"), True  # Integrated graphics can be intentional.
    canonical = normalize_gpu(gpu) or gpu
    table = DESKTOP_GPU_VALUES if product_type == "prebuilt" else LAPTOP_GPU_VALUES
    if canonical in table:
        return Decimal(table[canonical]), True
    return Decimal("0"), False


def estimate_fair_value(listing: Listing) -> tuple[Decimal, Decimal, str]:
    """Return ``(fair_value, value_index, confidence)`` for a listing."""
    product_type = listing.product_type
    cpu_cad, cpu_known = cpu_value(listing.cpu, product_type)
    gpu_cad, gpu_known = gpu_value(listing.gpu, product_type)

    if product_type == "prebuilt":
        platform_cad = Decimal("550")  # board/case/PSU/storage/OS/assembly
        ram_per_gb = Decimal("5")
    else:
        platform_cad = Decimal("650") if listing.cpu.startswith("M") else Decimal("550")
        ram_per_gb = Decimal("8") if listing.cpu.startswith("M") else Decimal("6")

    ram_cad = Decimal(min(listing.ram_gb or 0, 128)) * ram_per_gb
    raw_fair = platform_cad + cpu_cad + gpu_cad + ram_cad
    multiplier = CONDITION_MULTIPLIERS.get(
        listing.condition, CONDITION_MULTIPLIERS["used"]
    )
    fair = (raw_fair * multiplier).quantize(Decimal("0.01"))
    if listing.price_cad <= 0:
        index = Decimal("0")
    else:
        index = (fair / listing.price_cad * 100).quantize(Decimal("0.1"))

    complete = cpu_known and gpu_known and listing.ram_gb is not None
    confidence = "high" if complete else "medium"
    return fair, index, confidence


def rank_listings(
    listings: Iterable[Listing],
    *,
    product_type: str,
    max_price_cad: Decimal = Decimal("3000"),
    limit: int = 10,
) -> list[RankedDeal]:
    """Rank current listings best-first, enforcing type and price limits."""
    ranked: list[RankedDeal] = []
    seen: set[tuple[str, str]] = set()
    for listing in listings:
        identity = (listing.retailer, listing.sku)
        if identity in seen:
            continue
        seen.add(identity)
        if listing.product_type != product_type:
            continue
        if listing.price_cad <= 0 or listing.price_cad > max_price_cad:
            continue
        fair, index, confidence = estimate_fair_value(listing)
        ranked.append(RankedDeal(listing, fair, index, confidence))

    ranked.sort(
        key=lambda deal: (deal.value_index, -deal.listing.price_cad),
        reverse=True,
    )
    return ranked[:max(0, limit)]
