"""Shared component normalization for desktop/prebuilt listings."""

from __future__ import annotations

import re

from retailers._normalize import _strip


_RE_AMD_DESKTOP = re.compile(
    r"\b(?:amd\s+)?(?:ryzen\s+(?:[3579]|r[3579])|r[3579])[\s-]+"
    r"(\d{4}(?:x3d|xt|x|g|f)?)\b",
    re.IGNORECASE,
)
_RE_INTEL_ULTRA_DESKTOP = re.compile(
    r"\b(?:intel\s+)?(?:core\s+)?ultra\s+[3579][\s-]+"
    r"(\d{3}(?:k|kf|f|t)?)\b",
    re.IGNORECASE,
)
_RE_INTEL_CORE_DESKTOP = re.compile(
    r"\b(?:intel\s+)?(?:(?:core\s+)?i[3579]|ci[3579])[\s-]+"
    r"(\d{4,5}(?:k|kf|ks|f|t)?)\b",
    re.IGNORECASE,
)

_RE_NVIDIA = re.compile(
    r"\b(?:nvidia\s+|geforce\s+)?(?:geforce\s+)?"
    r"(rtx|gtx)\s*[\- ]?(\d{4})\s*(ti|super)?\b",
    re.IGNORECASE,
)
_RE_RADEON = re.compile(
    r"\b(?:amd\s+)?(?:radeon\s+)?rx\s*[\- ]?(\d{4})\s*(xt|gre|m)?\b",
    re.IGNORECASE,
)
_RE_INTEL_ARC = re.compile(r"\b(?:intel\s+)?arc\s+([ab]\d{3})\b", re.IGNORECASE)

# Require a system-memory marker so GPU VRAM ("RTX 5070 12GB") and storage
# capacities ("512GB SSD") are not mistaken for installed RAM.
_RAM_KIT_RE = re.compile(
    r"\b(\d{1,3})\s*GB\s*[xX]\s*(\d+)\s*(?:DDR[345X]*|LPDDR[45X]*)\b",
    re.I,
)
_RAM_PATTERNS = (
    re.compile(r"\b(\d{1,3})\s*GB\s*(?:DDR[345X]*|LPDDR[45X]*|RAM|Memory)\b", re.I),
    re.compile(r"\b(?:RAM|Memory)\s*[:\-]?\s*(\d{1,3})\s*GB\b", re.I),
    re.compile(
        r"\b(\d{1,3})\s*GB\s*(?:of\s+)?(?:system\s+|unified\s+)?memory\b",
        re.I,
    ),
)


def normalize_desktop_cpu(text: str) -> str | None:
    """Return a canonical desktop CPU model, or ``None``."""
    if not text:
        return None
    cleaned = _strip(text)
    for pattern in (
        _RE_AMD_DESKTOP,
        _RE_INTEL_ULTRA_DESKTOP,
        _RE_INTEL_CORE_DESKTOP,
    ):
        match = pattern.search(cleaned)
        if match:
            return match.group(1).upper()
    return None


def normalize_gpu(text: str) -> str | None:
    """Return a canonical NVIDIA, AMD, or Intel discrete GPU model."""
    if not text:
        return None
    cleaned = _strip(text)
    match = _RE_NVIDIA.search(cleaned)
    if match:
        family = match.group(1).upper()
        suffix = f" {match.group(3).title()}" if match.group(3) else ""
        return f"{family} {match.group(2)}{suffix}"
    match = _RE_RADEON.search(cleaned)
    if match:
        suffix = f" {match.group(2).upper()}" if match.group(2) else ""
        return f"RX {match.group(1)}{suffix}"
    match = _RE_INTEL_ARC.search(cleaned)
    if match:
        return f"Arc {match.group(1).upper()}"
    return None


def parse_system_ram(text: str) -> int | None:
    """Extract installed system RAM without confusing VRAM or SSD capacity."""
    kit = _RAM_KIT_RE.search(text or "")
    if kit:
        amount = int(kit.group(1)) * int(kit.group(2))
        if 4 <= amount <= 256:
            return amount
    for pattern in _RAM_PATTERNS:
        match = pattern.search(text or "")
        if match:
            amount = int(match.group(1))
            if 4 <= amount <= 256:
                return amount
    return None


def looks_like_prebuilt(title: str) -> bool:
    """Reject components and accept complete desktop/tower systems."""
    low = title.lower()
    if any(
        phrase in low
        for phrase in (
            "graphics card",
            "video card",
            "desktop memory",
            "motherboard",
            "barebone",
            "gpu only",
            "computer case",
        )
    ):
        return False
    return bool(
        re.search(
            r"\b(?:gaming\s+(?:desktop|pc|computer)|pc\s+desktop|"
            r"desktop\s+(?:computer|pc|tower)|prebuilt|workstation)\b",
            low,
        )
    )
