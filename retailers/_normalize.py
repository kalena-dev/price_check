"""CPU normalization.

Maps any retailer title string to a canonical CPU identifier matching the
forms used in config.yaml. Returns None if no known CPU is detected.

Canonical forms by family:
    AMD Ryzen HX / G       -> "9955HX3D", "8945HX", "7700G"
    Intel Core Ultra HX/H  -> "275HX", "265H", "388H"
    AMD Strix Halo         -> "AI Max+ Pro 395", "AI Max 390"
    AMD Ryzen AI HX/H      -> "HX_PRO 375", "HX 370", "H 465"
    Apple M-series         -> "M5 Max 18", "M4 Pro 14"
    Qualcomm Snapdragon X  -> "Snapdragon X Elite"
"""

from __future__ import annotations

import re

# Strip trademark/registered marks before matching.
_MARKS = re.compile(r"[™®©]")

# Hyphen-like Unicode chars Apple and others use in marketing copy.
# U+2010 hyphen, U+2011 non-breaking hyphen, U+2013 en dash, U+2014 em dash,
# U+2212 minus sign — all collapse to ASCII '-' for regex matching.
_HYPHENS = str.maketrans({
    "‐": "-",
    "‑": "-",
    "–": "-",
    "—": "-",
    "−": "-",
})


def _strip(text: str) -> str:
    return _MARKS.sub("", text).translate(_HYPHENS)


# --- Pattern handlers, ordered most-specific to least-specific. -------------
#
# Each handler takes the cleaned text, returns canonical str or None.


_RE_STRIX_HALO = re.compile(
    r"\b(?:ryzen\s+)?ai\s+max(\+)?\s+(pro\s+)?(\d{3})\b",
    re.IGNORECASE,
)


def _match_strix_halo(text: str) -> str | None:
    m = _RE_STRIX_HALO.search(text)
    if not m:
        return None
    plus = "+" if m.group(1) else ""
    pro = " Pro" if m.group(2) else ""
    return f"AI Max{plus}{pro} {m.group(3)}"


# AMD Ryzen AI HX / H — match BEFORE plain Intel Ultra so "AI 9 H 465" wins.
# Form: "Ryzen AI 9 HX PRO 375", "AI 9 HX 370", "AI 9 H 465"
_RE_RYZEN_AI_HX = re.compile(
    r"\b(?:ryzen\s+)?ai\s+\d\s+(hx\s+pro|hx|h)\s+(\d{3})\b",
    re.IGNORECASE,
)


def _match_ryzen_ai_hx(text: str) -> str | None:
    m = _RE_RYZEN_AI_HX.search(text)
    if not m:
        return None
    suffix = m.group(1).upper().replace(" ", "_")
    return f"{suffix} {m.group(2)}"


# AMD Ryzen Fire Range / Dragon Range / classic 4-digit HX/HS/G with optional 3D
# Form: "Ryzen 9 9955HX3D", "AMD Ryzen 7 8745HX", "Ryzen 9-8945HX"
_RE_RYZEN_4D = re.compile(
    r"\b(?:amd\s+)?(?:ryzen\s+\d|r\d)[\s\-]*(\d{4}(?:hx|hs|g)(?:3d)?)\b",
    re.IGNORECASE,
)


def _match_ryzen_4d(text: str) -> str | None:
    m = _RE_RYZEN_4D.search(text)
    if not m:
        return None
    return m.group(1).upper()


# Intel Core Ultra Arrow Lake / Meteor Lake / Lunar Lake HX / H / U
# Form: "Core Ultra 9 275HX", "Ultra 7 265H", "Core Ultra X9 388H", "Ultra 5 235HX"
_RE_INTEL_ULTRA = re.compile(
    r"\b(?:core\s+)?ultra\s+x?\d\s+(\d{3}(?:hx|hk|h|u))\b",
    re.IGNORECASE,
)


def _match_intel_ultra(text: str) -> str | None:
    m = _RE_INTEL_ULTRA.search(text)
    if not m:
        return None
    return m.group(1).upper()


# Apple M4/M5 Pro/Max with core count.
# Two accepted forms:
#   short: "M5 Max 18-Core", "M4 Pro 14 cœurs"
#   long:  "Apple M4 Max chip with 16-core CPU and 40-core GPU"
# We require the core-count to be 8/10/12/14/16/18/20 to avoid matching
# unrelated numbers in long titles.
_RE_APPLE_SHORT = re.compile(
    r"\b(m[45])\s+(pro|max)\s+(\d{2})[\s\-]?(?:cores?|cœurs?|coeurs?)\b",
    re.IGNORECASE,
)
_RE_APPLE_LONG = re.compile(
    r"\b(m[45])\s+(pro|max)\b[^.|]{1,40}?"
    r"(\d{2})[\s\-]?(?:cores?|cœurs?|coeurs?)\s*CPU",
    re.IGNORECASE,
)


def _match_apple(text: str) -> str | None:
    m = _RE_APPLE_SHORT.search(text) or _RE_APPLE_LONG.search(text)
    if not m:
        return None
    chip = m.group(1).upper()
    variant = m.group(2).title()
    cores = m.group(3)
    return f"{chip} {variant} {cores}"


# Qualcomm Snapdragon X Elite / Plus
# Form: "Snapdragon X Elite X1E001DE", "Snapdragon X Plus"
_RE_SNAPDRAGON = re.compile(
    r"\bsnapdragon\s+x\s+(elite|plus)\b",
    re.IGNORECASE,
)


def _match_snapdragon(text: str) -> str | None:
    m = _RE_SNAPDRAGON.search(text)
    if not m:
        return None
    return f"Snapdragon X {m.group(1).title()}"


# --- Public API -------------------------------------------------------------

# Order matters: most-specific first.
_HANDLERS = (
    _match_strix_halo,
    _match_ryzen_ai_hx,
    _match_ryzen_4d,
    _match_intel_ultra,
    _match_apple,
    _match_snapdragon,
)


def normalize_cpu(text: str) -> str | None:
    """Return canonical CPU string for a retailer title, or None."""
    if not text:
        return None
    cleaned = _strip(text)
    for handler in _HANDLERS:
        result = handler(cleaned)
        if result:
            return result
    return None
