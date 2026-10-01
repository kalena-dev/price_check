"""Conservative, shared stock-availability parsing helpers.

Only explicit retailer data is treated as proof that an item is purchasable.
Missing, malformed, or merely descriptive uses of words such as "available"
remain ``unknown`` so callers never accidentally advertise an item as being
in stock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from retailers.base import Availability


_IN_STOCK_VALUES = {
    "available",
    "availableforpickup",
    "availableinstore",
    "availableonline",
    "availabletoship",
    "instock",
    "instockonline",
    "limitedstock",
    "lowstock",
    "readyforpickup",
}
_OUT_OF_STOCK_VALUES = {
    "noinventory",
    "notavailable",
    "notavailableatthislocation",
    "notavailableforpickup",
    "notavailableinstore",
    "notavailableonline",
    "outofstock",
    "soldout",
    "temporarilyoutofstock",
    "temporarilyunavailable",
    "unavailable",
}

_OUT_OF_STOCK_TEXT_RE = re.compile(
    r"\b(?:"
    r"out[\s-]*of[\s-]*stock"
    r"|not[\s-]+in[\s-]+stock"
    r"|no[\s-]+stock"
    r"|sold[\s-]*out"
    r"|(?:currently|temporarily)\s+unavailable"
    r"|unavailable"
    r"|not\s+available(?:\s+(?:online|in[\s-]*stores?|for\s+(?:delivery|pickup)))?"
    r")\b",
    re.IGNORECASE,
)
_IN_STOCK_TEXT_RE = re.compile(
    r"\b(?:"
    r"in[\s-]*stock"
    r"|available\s+(?:online|in[\s-]*stores?|for\s+(?:delivery|pickup))"
    r"|ready\s+for\s+pickup"
    r")\b",
    re.IGNORECASE,
)
_PURCHASE_ACTION_RE = re.compile(
    r"\b(?:add\s+to\s+(?:cart|bag)|buy\s+now|purchase)\b",
    re.IGNORECASE,
)


def _normalized_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Schema.org availability values are commonly full URLs.
    value = value.strip().rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def parse_availability(value: object) -> Availability:
    """Parse one explicit status value; unrecognized values stay unknown."""
    normalized = _normalized_value(value)
    if normalized in _OUT_OF_STOCK_VALUES:
        return "out_of_stock"
    if normalized in _IN_STOCK_VALUES:
        return "in_stock"
    return "unknown"


def first_known_availability(values: Iterable[object]) -> Availability:
    """Return the first recognized status, preserving source precedence."""
    for value in values:
        availability = parse_availability(value)
        if availability != "unknown":
            return availability
    return "unknown"


def availability_from_text(
    text: object,
    *,
    has_purchase_action: bool | None = None,
) -> Availability:
    """Read explicit stock phrases from card-local text.

    A plain word such as "available" is intentionally not enough.  A known
    sold-out phrase wins over positive evidence when a stale card contains
    conflicting controls and labels.
    """
    if not isinstance(text, str):
        return "unknown"
    if _OUT_OF_STOCK_TEXT_RE.search(text):
        return "out_of_stock"
    if _IN_STOCK_TEXT_RE.search(text):
        return "in_stock"
    if has_purchase_action is True:
        return "in_stock"
    return "unknown"


def _is_disabled_control(control) -> bool:
    try:
        if control.has_attr("disabled"):
            return True
        if str(control.get("aria-disabled") or "").casefold() == "true":
            return True
        classes = control.get("class") or []
    except Exception:
        return True
    if isinstance(classes, str):
        classes = classes.split()
    return any(str(token).casefold() == "disabled" for token in classes)


def _has_enabled_purchase_action(card) -> bool:
    try:
        controls = card.select("button, input, a, [role='button']")
    except Exception:
        return False
    for control in controls:
        try:
            text = " ".join(
                part
                for part in (
                    control.get_text(" ", strip=True),
                    str(control.get("value") or ""),
                    str(control.get("aria-label") or ""),
                    str(control.get("title") or ""),
                )
                if part
            )
        except Exception:
            continue
        if _PURCHASE_ACTION_RE.search(text) and not _is_disabled_control(control):
            return True
    return False


def availability_from_html_card(card) -> Availability:
    """Parse availability using only one BeautifulSoup product card/tile."""
    if card is None:
        return "unknown"

    status_values: list[object] = []
    try:
        elements = [card, *card.find_all(True)]
    except Exception:
        elements = [card]
    for element in elements:
        try:
            attrs = element.attrs
        except Exception:
            continue
        for key in (
            "availability",
            "data-availability",
            "data-stock-status",
            "data-stock",
            "data-inventory-status",
            "content",
            "href",
        ):
            value = attrs.get(key)
            # content/href can mean anything unless this is an explicit
            # schema.org availability node.
            if key in {"content", "href"}:
                itemprop = str(attrs.get("itemprop") or "").casefold()
                if "availability" not in itemprop:
                    continue
            status_values.append(value)
        classes = attrs.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        # CSS classes are weaker evidence than a dedicated availability
        # attribute.  In particular, a generic ``available`` class can refer
        # to a configurable feature rather than inventory.
        status_values.extend(
            value
            for value in classes
            if any(
                marker in _normalized_value(value)
                for marker in ("stock", "soldout", "unavailable", "notavailable")
            )
        )

    parsed = [parse_availability(value) for value in status_values]
    if "out_of_stock" in parsed:
        return "out_of_stock"

    try:
        text = card.get_text(" ", strip=True)
    except Exception:
        text = ""
    text_availability = availability_from_text(
        text,
        has_purchase_action=_has_enabled_purchase_action(card),
    )
    if text_availability == "out_of_stock":
        return "out_of_stock"
    if "in_stock" in parsed or text_availability == "in_stock":
        return "in_stock"
    return "unknown"


def availability_from_mapping(
    data: object,
    *,
    status_keys: Iterable[str],
    in_stock_keys: Iterable[str] = (),
    out_of_stock_keys: Iterable[str] = (),
) -> Availability:
    """Parse explicit status fields and strict boolean flags from one card.

    For positive flags, ``False`` is also explicit out-of-stock evidence.
    For negative flags, only ``True`` is evidence.  String values such as
    ``"false"`` are deliberately ignored rather than relying on truthiness.
    """
    if not isinstance(data, Mapping):
        return "unknown"

    lowered = {str(key).casefold(): value for key, value in data.items()}
    states: list[Availability] = []
    for key in status_keys:
        states.append(parse_availability(lowered.get(key.casefold())))
    for key in in_stock_keys:
        value = lowered.get(key.casefold())
        if isinstance(value, bool):
            states.append("in_stock" if value else "out_of_stock")
    for key in out_of_stock_keys:
        value = lowered.get(key.casefold())
        if isinstance(value, bool) and value:
            states.append("out_of_stock")

    # On contradictory card data, avoid claiming that the item is in stock.
    if "out_of_stock" in states:
        return "out_of_stock"
    if "in_stock" in states:
        return "in_stock"
    return "unknown"


def walmart_availability(item: object, detail: object = None) -> Availability:
    """Parse Walmart status, with recognized detail values taking priority."""
    detail_values: list[object] = []
    if isinstance(detail, Mapping):
        detail_product = detail.get("product")
        if isinstance(detail_product, Mapping):
            detail_values.extend(
                (
                    detail_product.get("availabilityStatus"),
                    detail_product.get("availabilityStatusDisplayValue"),
                )
            )
        detail_values.extend(
            (
                detail.get("availabilityStatus"),
                detail.get("availabilityStatusDisplayValue"),
            )
        )
    parsed_detail = first_known_availability(detail_values)
    if parsed_detail != "unknown":
        return parsed_detail

    if not isinstance(item, Mapping):
        return "unknown"
    return first_known_availability(
        (
            item.get("availabilityStatus"),
            item.get("availabilityStatusDisplayValue"),
        )
    )


def _channel_status(value: object) -> Availability:
    if isinstance(value, Mapping):
        return first_known_availability(
            value.get(key)
            for key in ("status", "state", "availability", "availabilityStatus")
        )
    return parse_availability(value)


def _bestbuy_source_availability(source: object) -> Availability:
    if not isinstance(source, Mapping):
        return "unknown"

    nested = source.get("availability")
    online_values: list[object] = []
    store_values: list[object] = []
    overall_values: list[object] = []
    containers = [source]
    if isinstance(nested, Mapping):
        containers.insert(0, nested)
    elif nested is not None:
        overall_values.append(nested)

    positive = False
    online_negative = False
    for container in containers:
        overall_values.extend(
            container.get(key)
            for key in ("availabilityStatus", "status")
        )
        online_values.extend(
            container.get(key)
            for key in ("onlineAvailability", "online")
        )
        store_values.extend(
            container.get(key)
            for key in ("inStoreAvailability", "inStore", "pickupAvailability")
        )

        for key in ("isAvailableOnline", "isAvailableForPickup", "isAvailableInStore"):
            value = container.get(key)
            if value is True:
                positive = True
            elif key == "isAvailableOnline" and value is False:
                online_negative = True

    overall_states = [parse_availability(value) for value in overall_values]
    online_states = [_channel_status(value) for value in online_values]
    store_states = [_channel_status(value) for value in store_values]

    if (
        positive
        or "in_stock" in overall_states
        or "in_stock" in online_states
        or "in_stock" in store_states
    ):
        return "in_stock"
    if "out_of_stock" in overall_states:
        return "out_of_stock"
    if online_negative or "out_of_stock" in online_states:
        # A sold-out online channel is globally unavailable unless pickup or
        # in-store data contains explicit positive evidence (handled above).
        return "out_of_stock"
    return "unknown"


def bestbuy_availability(
    product: object,
    detail: object = None,
) -> Availability:
    """Parse Best Buy availability, preferring recognized detail evidence."""
    detail_availability = _bestbuy_source_availability(detail)
    if detail_availability != "unknown":
        return detail_availability
    return _bestbuy_source_availability(product)
