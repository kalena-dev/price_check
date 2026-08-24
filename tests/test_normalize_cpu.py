"""Tests for retailers/_normalize.py — CPU title normalization."""

from __future__ import annotations

import pytest

from retailers._normalize import normalize_cpu


# Each tuple: (input_title, expected_canonical)
POSITIVES = [
    # AMD Ryzen 4-digit HX / G family.
    ("AMD Ryzen 9 8945HX", "8945HX"),
    ("AMD Ryzen™ 9 8945HX", "8945HX"),
    ("AMD Ryzen 7 8745HX", "8745HX"),
    ("Ryzen 9-9955HX3D", "9955HX3D"),
    ("AMD Ryzen 9 9955HX", "9955HX"),
    ("Ryzen 9 9850HX", "9850HX"),
    ("Ryzen 7 7840HX", "7840HX"),
    ("Ryzen 7 7700G", "7700G"),
    ("R9 8940HX laptop", "8940HX"),

    # Intel Core Ultra HX / H.
    ("Intel Core Ultra 9 275HX", "275HX"),
    ("Core Ultra 9 285HX", "285HX"),
    ("Core Ultra 9 290HX laptop", "290HX"),
    ("Intel Core Ultra 7 265HX", "265HX"),
    ("Intel Core Ultra 5 265HX", "265HX"),
    ("Core Ultra 5 235HX", "235HX"),
    ("Intel Core Ultra 7 265H", "265H"),
    ("Intel Core Ultra 9 388H", "388H"),
    ("Core Ultra X9 388H", "388H"),
    ("Core Ultra 7 358H", "358H"),
    ("Core Ultra 7 356H", "356H"),

    # AMD Strix Halo (AI Max).
    ("AMD Ryzen AI Max+ 395", "AI Max+ 395"),
    ("Ryzen AI Max+ Pro 395", "AI Max+ Pro 395"),
    ("AMD RYZEN AI MAX+ 392", "AI Max+ 392"),
    ("AI Max 390 laptop", "AI Max 390"),
    ("Ryzen AI Max Pro 390", "AI Max Pro 390"),
    ("AMD Ryzen AI Max 385", "AI Max 385"),
    ("AMD Ryzen AI Max Pro 385", "AI Max Pro 385"),

    # AMD Ryzen AI HX / H.
    ("AMD Ryzen AI 9 HX 370", "HX 370"),
    ("Ryzen AI 9 HX 375", "HX 375"),
    ("AMD Ryzen AI 9 HX PRO 370", "HX_PRO 370"),
    ("AMD Ryzen AI 9 HX PRO 375", "HX_PRO 375"),
    ("AMD Ryzen AI 9 HX PRO 470", "HX_PRO 470"),
    ("Ryzen AI 9 HX 470", "HX 470"),
    ("AMD Ryzen AI 9 H 465", "H 465"),

    # Apple M-series.
    ("MacBook Pro Apple M5 Max 18-Core", "M5 Max 18"),
    ("Apple M5 Pro 18 Core", "M5 Pro 18"),
    ("Apple M5 Pro 15-Core", "M5 Pro 15"),
    ("Apple M4 Max 16 core", "M4 Max 16"),
    ("Apple M4 Max 14-Core", "M4 Max 14"),
    ("Apple M4 Pro 14 Core", "M4 Pro 14"),
    ("Apple M4 Pro 12-Core", "M4 Pro 12"),
    # Apple's marketing-copy form (long).
    ("MacBook Pro Apple M4 Max chip with 16-core CPU and 40-core GPU", "M4 Max 16"),
    ("Apple M5 Pro chip with 14-core CPU and 20-core GPU", "M5 Pro 14"),
    ("M4 Pro with 12-core CPU and 16-core GPU", "M4 Pro 12"),

    # Intel Core i-series Raptor Lake-HX (last-gen flagships, still common in
    # 2025 inventory).
    ("Intel Core i9-14900HX", "14900HX"),
    ("Core i9-13900HX", "13900HX"),
    ("Intel Core i7-12700H", "12700H"),
    ("Intel Core i9 13950HX", "13950HX"),
    ("Core i7-13700HX", "13700HX"),
    ("Intel Core i9-13980HX", "13980HX"),
    ("i9-14650HX laptop", "14650HX"),

    # Apple M3 Pro/Max (last-gen flagship Apple Silicon).
    ("Apple M3 Max 16-Core", "M3 Max 16"),
    ("MacBook Pro Apple M3 Max chip with 16-core CPU and 40-core GPU", "M3 Max 16"),
    ("Apple M3 Pro 12-Core", "M3 Pro 12"),
    ("M3 Pro 11 Core", "M3 Pro 11"),

    # French-language variants (Quebec retailers like La Source, BB CA fr-CA).
    ("Processeur Intel Core Ultra 9 275HX", "275HX"),
    ("Ordinateur portable AMD Ryzen 9 8945HX", "8945HX"),
    ("MacBook Pro avec puce Apple M5 Pro 14 cœurs", "M5 Pro 14"),
    ("MacBook Pro avec puce M4 Max 16 coeurs", "M4 Max 16"),
]


# Strings that must NOT match — chips outside the families we recognize.
NEGATIVES = [
    "AMD Ryzen 7 5800H",
    "AMD Athlon Silver 3050U",
    "MacBook Air M2",
    "16GB RAM 1TB SSD",
    "Gaming Laptop with RTX 4090",
    "Qualcomm Snapdragon X Elite laptop",
    "Snapdragon X Plus notebook",
    "",
    "   ",
]


@pytest.mark.parametrize("title,expected", POSITIVES)
def test_normalizer_matches_expected_form(title: str, expected: str) -> None:
    assert normalize_cpu(title) == expected


@pytest.mark.parametrize("title", NEGATIVES)
def test_normalizer_returns_none_for_unrelated(title: str) -> None:
    assert normalize_cpu(title) is None


def test_handler_priority_ai_max_beats_ryzen_4d() -> None:
    # If both could theoretically match, Strix Halo handler runs first.
    assert normalize_cpu("AMD Ryzen AI Max+ 395 (no 4-digit chip here)") == "AI Max+ 395"


def test_handler_priority_apple_not_confused_by_pro_keyword() -> None:
    # "MacBook Pro" contains "Pro" but Apple regex requires Mx Pro/Max + digits + core.
    assert normalize_cpu("MacBook Pro 14-inch (no chip listed)") is None
