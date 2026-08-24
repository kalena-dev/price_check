"""Unit tests for retailer parsing and coverage helpers."""

from __future__ import annotations

import json
from decimal import Decimal

from retailers.bestbuy_ca import BestBuyCA
from retailers.walmart_ca import WalmartCA, _extract_next_data, _search_result


def _bestbuy_product(sku: str, name: str) -> dict:
    return {
        "sku": sku,
        "name": name,
        "salePrice": 1999.99,
        "productUrl": f"/en-ca/product/example/{sku}",
        "thumbnailImage": "https://example.com/product.jpg",
    }


def test_bestbuy_detail_specs_resolve_ambiguous_apple_cpu() -> None:
    product = _bestbuy_product(
        "18619910",
        'Apple MacBook Pro 14.2" (Apple M4 Pro / 24GB RAM / 1TB SSD)',
    )
    detail = {
        "name": product["name"],
        "availability": {
            "onlineAvailability": "InStock",
            "inStoreAvailability": "NotAvailableAtThisLocation",
        },
        "specs": [
            {"name": "Processor Type", "value": "Apple M4 Pro"},
            {"name": "Processor Cores", "value": "14"},
            {"name": "RAM Size", "value": "24 GB"},
        ],
    }

    listing = BestBuyCA()._parse_product(product, detail)

    assert listing is not None
    assert listing.cpu == "M4 Pro 14"
    assert listing.ram_gb == 24


def test_bestbuy_search_paginates_and_deduplicates() -> None:
    class Response:
        status_code = 200

        def __init__(self, payload: dict):
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    class Client:
        def __init__(self):
            self.pages: list[int] = []

        def get(self, _url: str, params: dict):
            page = params["page"]
            self.pages.append(page)
            first = _bestbuy_product(
                "A", "Lenovo gaming laptop Intel Core Ultra 9 275HX 32GB RTX 5070"
            )
            second = _bestbuy_product(
                "B", "ASUS gaming laptop AMD Ryzen 9 8945HX 16GB RTX 5060"
            )
            products = [first] if page == 1 else [first, second]
            return Response({"products": products, "totalPages": 2})

    client = Client()
    listings = BestBuyCA()._search_one(client, "gaming", set(), {})

    assert client.pages == [1, 2]
    assert [listing.sku for listing in listings] == ["A", "B"]


def test_walmart_next_data_and_product_parser() -> None:
    item = {
        "usItemId": "ABC123",
        "name": (
            'Lenovo Legion 5i 16" Gaming Laptop - Intel Core Ultra 9 275HX - '
            "32GB RAM - RTX 5070"
        ),
        "price": 2499.99,
        "canonicalUrl": "/en/ip/lenovo-legion/ABC123",
        "image": "https://example.com/laptop.jpg",
        "availabilityStatusDisplayValue": "In stock",
        "category": {"path": [{"name": "Electronics"}, {"name": "Gaming Laptops"}]},
    }
    payload = {
        "props": {
            "pageProps": {
                "initialData": {
                    "searchResult": {
                        "itemStacks": [{"items": [item]}],
                        "hasMorePages": False,
                    }
                }
            }
        }
    }
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )

    data = _extract_next_data(html)
    assert data is not None
    assert _search_result(data) is not None

    listing = WalmartCA()._parse_product(item, {"275HX"})
    assert listing is not None
    assert listing.cpu == "275HX"
    assert listing.price_cad == Decimal("2499.99")
    assert listing.ram_gb == 32
    assert listing.gpu == "RTX 5070"
    assert listing.url.startswith("https://www.walmart.ca/")


def test_walmart_detail_specs_resolve_ambiguous_apple_cpu() -> None:
    item = {
        "usItemId": "MAC123",
        "name": (
            'Open Box - Apple MacBook Pro 16" Laptop '
            "(Apple M4 Pro / 24GB RAM / 512GB SSD)"
        ),
        "price": 2299.99,
        "category": {"path": [{"name": "MacBook Laptops"}]},
    }
    detail = {
        "product": {"availabilityStatus": "IN_STOCK"},
        "idml": {
            "productHighlights": [
                {"name": "Processor Type", "value": "Apple M4 Pro"},
                {"name": "Processor Core Count", "value": "14"},
                {"name": "RAM Memory", "value": "24 GB"},
            ]
        },
    }

    listing = WalmartCA()._parse_product(item, {"M4 Pro 14"}, detail)

    assert listing is not None
    assert listing.cpu == "M4 Pro 14"
    assert listing.condition == "open_box"
    assert listing.ram_gb == 24


def test_walmart_parser_rejects_cpu_outside_allowlist() -> None:
    item = {
        "usItemId": "ABC123",
        "name": "Gaming Laptop AMD Ryzen 9 8945HX 32GB RAM",
        "price": 1800,
        "category": {"path": [{"name": "Gaming Laptops"}]},
    }

    assert WalmartCA()._parse_product(item, {"275HX"}) is None
