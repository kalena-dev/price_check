"""Stock-state parsing tests for retailer-local evidence."""

from __future__ import annotations

from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from retailers._availability import (
    availability_from_html_card,
    availability_from_mapping,
    availability_from_text,
    bestbuy_availability,
    parse_availability,
    walmart_availability,
)
from retailers.apple_ca import AppleCA
from retailers.base import Listing
from retailers.bestbuy_ca import BestBuyCA
from retailers.bestbuy_prebuilts import BestBuyPrebuilts
from retailers.canadacomputers import CanadaComputers
from retailers.lenovo_ca import LenovoCA
from retailers.memoryexpress import MemoryExpress
from retailers.newegg_ca import NeweggCA
from retailers.redflagdeals import RedFlagDeals
from retailers.visions_ca import VisionsCA
from retailers.walmart_ca import WalmartCA
from retailers.walmart_prebuilts import WalmartPrebuilts


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("IN_STOCK", "in_stock"),
        ("In stock", "in_stock"),
        ("https://schema.org/InStock", "in_stock"),
        ("SOLD_OUT", "out_of_stock"),
        ("Out of stock", "out_of_stock"),
        ("NotAvailableAtThisLocation", "out_of_stock"),
        (None, "unknown"),
        (False, "unknown"),
        ("ships in two days", "unknown"),
        ("This processor is available with 32 GB RAM", "unknown"),
    ],
)
def test_explicit_availability_values(value: object, expected: str) -> None:
    assert parse_availability(value) == expected


def test_listing_defaults_to_unknown_availability() -> None:
    listing = Listing(
        retailer="example",
        sku="1",
        url="https://example.com/1",
        title="Laptop",
        cpu="275HX",
        ram_gb=32,
        gpu="RTX 5070",
        price_cad=Decimal("1999"),
        image_url=None,
    )

    assert listing.availability == "unknown"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("In stock online", "in_stock"),
        ("Available for pickup", "in_stock"),
        ("Out of stock", "out_of_stock"),
        ("Not in stock", "out_of_stock"),
        ("No stock", "out_of_stock"),
        ("Unavailable", "out_of_stock"),
        ("A configuration available with RTX graphics", "unknown"),
        ("Add to cart", "unknown"),
    ],
)
def test_card_text_requires_explicit_stock_evidence(text: str, expected: str) -> None:
    assert availability_from_text(text) == expected


def test_html_card_stock_evidence_is_local_and_conservative() -> None:
    soup = BeautifulSoup(
        """
        <main>
          <div id="sold"><span>Out of stock</span><button>Add to cart</button></div>
          <div id="buy"><button>Add to cart</button></div>
          <div id="disabled"><button disabled>Add to cart</button></div>
          <div id="schema"><link itemprop="availability"
               href="https://schema.org/InStock"></div>
          <div id="generic" class="available">Memory available in 16 GB and 32 GB sizes</div>
        </main>
        """,
        "html.parser",
    )

    assert availability_from_html_card(soup.select_one("#sold")) == "out_of_stock"
    assert availability_from_html_card(soup.select_one("#buy")) == "in_stock"
    assert availability_from_html_card(soup.select_one("#disabled")) == "unknown"
    assert availability_from_html_card(soup.select_one("#schema")) == "in_stock"
    assert availability_from_html_card(soup.select_one("#generic")) == "unknown"


def test_mapping_flags_are_strict_booleans_and_conflicts_are_conservative() -> None:
    kwargs = {
        "status_keys": ("stockStatus",),
        "in_stock_keys": ("isInStock",),
        "out_of_stock_keys": ("isSoldOut",),
    }
    assert availability_from_mapping({"isInStock": True}, **kwargs) == "in_stock"
    assert availability_from_mapping({"isInStock": False}, **kwargs) == "out_of_stock"
    assert availability_from_mapping({"isInStock": "false"}, **kwargs) == "unknown"
    assert (
        availability_from_mapping(
            {"stockStatus": "InStock", "isSoldOut": True}, **kwargs
        )
        == "out_of_stock"
    )


@pytest.mark.parametrize(
    ("product", "detail", "expected"),
    [
        ({}, None, "unknown"),
        ({"availabilityStatusDisplayValue": "In stock"}, None, "in_stock"),
        ({"availabilityStatus": "OUT_OF_STOCK"}, None, "out_of_stock"),
        (
            {"availabilityStatus": "OUT_OF_STOCK"},
            {"product": {"availabilityStatus": "IN_STOCK"}},
            "in_stock",
        ),
        (
            {"availabilityStatus": "IN_STOCK"},
            {"product": {"availabilityStatusDisplayValue": "Sold out"}},
            "out_of_stock",
        ),
        (
            {"availabilityStatus": "IN_STOCK"},
            {"product": {"availabilityStatus": "unexpected"}},
            "in_stock",
        ),
    ],
)
def test_walmart_detail_status_precedence(
    product: dict, detail: dict | None, expected: str
) -> None:
    assert walmart_availability(product, detail) == expected


@pytest.mark.parametrize(
    ("product", "detail", "expected"),
    [
        ({}, None, "unknown"),
        ({"isAvailableOnline": True}, None, "in_stock"),
        ({"isAvailableOnline": False}, None, "out_of_stock"),
        ({"isAvailableOnline": "false"}, None, "unknown"),
        ({"isAvailableForPickup": False}, None, "unknown"),
        (
            {"availability": {"onlineAvailability": "SoldOut"}},
            None,
            "out_of_stock",
        ),
        (
            {"availability": {"onlineAvailability": "SoldOut"}},
            {"isAvailableForPickup": True},
            "in_stock",
        ),
        (
            {
                "availability": {
                    "online": {"state": "SoldOut"},
                    "inStore": {"status": "InStock"},
                }
            },
            None,
            "in_stock",
        ),
        (
            {
                "availability": {
                    "onlineAvailability": "SoldOut",
                    "inStoreAvailability": "NotAvailableAtThisLocation",
                }
            },
            None,
            "out_of_stock",
        ),
        (
            {"availability": {"onlineAvailability": "InStock"}},
            {"availability": {"onlineAvailability": "SoldOut"}},
            "out_of_stock",
        ),
        (
            {"availability": {"onlineAvailability": "InStock"}},
            {"availability": {"onlineAvailability": {"bad": "shape"}}},
            "in_stock",
        ),
    ],
)
def test_bestbuy_channel_and_boolean_availability(
    product: dict, detail: dict | None, expected: str
) -> None:
    assert bestbuy_availability(product, detail) == expected


def test_walmart_parsers_return_out_of_stock_snapshots() -> None:
    laptop = {
        "usItemId": "WM-LAPTOP",
        "name": "Gaming Laptop Intel Core Ultra 9 275HX 32GB RAM RTX 5070",
        "price": 2499.99,
        "availabilityStatus": "OUT_OF_STOCK",
        "category": {"path": [{"name": "Gaming Laptops"}]},
    }
    desktop = {
        "usItemId": "WM-PC",
        "name": "Gaming Desktop AMD Ryzen 7 7700X RTX 5070 32GB RAM",
        "price": 2099.99,
        "availabilityStatusDisplayValue": "Sold out",
    }

    laptop_listing = WalmartCA()._parse_product(laptop, {"275HX"})
    desktop_listing = WalmartPrebuilts()._parse_product(desktop)

    assert laptop_listing is not None
    assert laptop_listing.availability == "out_of_stock"
    assert desktop_listing is not None
    assert desktop_listing.availability == "out_of_stock"


def test_bestbuy_parsers_return_out_of_stock_snapshots() -> None:
    laptop = {
        "sku": "BB-LAPTOP",
        "name": "Gaming Laptop Intel Core Ultra 9 275HX 32GB RAM RTX 5070",
        "salePrice": 2499.99,
        "availability": {"onlineAvailability": "SoldOut"},
    }
    desktop = {
        "sku": "BB-PC",
        "name": "Gaming Desktop AMD Ryzen 7 7700X RTX 5070 32GB RAM",
        "salePrice": 2099.99,
        "availability": {"onlineAvailability": "NotAvailable"},
    }

    laptop_listing = BestBuyCA()._parse_product(laptop)
    desktop_listing = BestBuyPrebuilts()._parse_product(desktop)

    assert laptop_listing is not None
    assert laptop_listing.availability == "out_of_stock"
    assert desktop_listing is not None
    assert desktop_listing.availability == "out_of_stock"


def test_newegg_parses_card_local_stock_flag() -> None:
    product = {
        "ItemCell": {
            "Item": "34-123-456",
            "Description": {
                "Title": "Gaming Laptop Intel Core Ultra 9 275HX 32GB RTX 5070"
            },
            "Subcategory": {"SubcategoryDescription": "Gaming Laptops"},
            "UnitCost": 2199.99,
            "IsSoldOut": True,
        }
    }

    listing = NeweggCA()._parse_product(product)

    assert listing is not None
    assert listing.availability == "out_of_stock"


@pytest.mark.parametrize(
    ("adapter", "html", "selector", "args"),
    [
        (
            MemoryExpress(),
            """
            <div class="c-prod-grid__item" data-product-id="ME1">
              <a class="c-prod-grid__item-name" href="/Products/ME1">
                Gaming Laptop Intel Core Ultra 9 275HX 32GB RTX 5070
              </a>
              <div class="c-prod-grid__item-price">$2,199.99</div>
              <span>Out of stock</span>
            </div>
            """,
            ".c-prod-grid__item",
            (),
        ),
        (
            CanadaComputers(),
            """
            <div class="product-desc-box">
              <a class="product-desc" href="/gaming-laptops/12345/example">
                <div class="product-desc-title">Gaming Laptop Intel Core Ultra 9 275HX 32GB RTX 5070</div>
              </a>
              <span class="c-DA0000">$2,299.99</span>
              <span>Unavailable</span>
            </div>
            """,
            ".product-desc-box",
            (),
        ),
        (
            AppleCA(),
            """
            <li>
              <h3>MacBook Pro Apple M4 Pro with 14-core CPU and 24GB RAM</h3>
              <a href="/ca/shop/product/ABC123/LLA/example">View</a>
              <span>Sold out</span>
              <div class="as-producttile-currentprice">$2,499.00</div>
            </li>
            """,
            ".as-producttile-currentprice",
            ("https://www.apple.com/ca/shop", "new"),
        ),
    ],
)
def test_html_adapters_keep_sold_out_cards(
    adapter, html: str, selector: str, args: tuple
) -> None:
    node = BeautifulSoup(html, "html.parser").select_one(selector)
    if isinstance(adapter, AppleCA):
        listing = adapter._parse_tile(node, *args)
    else:
        listing = adapter._parse_card(node, *args)

    assert listing is not None
    assert listing.availability == "out_of_stock"


def test_redflagdeals_thread_remains_unverified() -> None:
    subject = BeautifulSoup(
        """
        <div class="post_subject">
          <a class="post_subject_link" href="deal-example-1234567/">
            [Newegg] Gaming Laptop Intel Core Ultra 9 275HX $1,999
          </a>
        </div>
        """,
        "html.parser",
    ).select_one(".post_subject")

    listing = RedFlagDeals()._parse_post(subject)

    assert listing is not None
    assert listing.availability == "unknown"


def test_playwright_adapters_parse_card_local_stock_text() -> None:
    class Anchor:
        def get_attribute(self, _name: str) -> str:
            return "/ca/en/p/LEGION"

    class Anchors:
        first = Anchor()

    class Card:
        def locator(self, _selector: str) -> Anchors:
            return Anchors()

        def evaluate(self, _script: str) -> bool:
            return False

    lenovo = LenovoCA()._parse_card(
        "LEGION",
        "Legion Pro 7 Intel Core Ultra 9 275HX 32GB RTX 5070\n$2,399.99\nOut of stock",
        Card(),
    )
    visions, used_detail = VisionsCA()._parse_row(
        "Gaming Laptop Intel Core Ultra 9 275HX 32GB RTX 5070",
        "https://www.visions.ca/product/example",
        "Example (VIS123) Special Price $2,299.99 Not in stock",
        None,
    )

    assert lenovo is not None and lenovo.availability == "out_of_stock"
    assert visions is not None and visions.availability == "out_of_stock"
    assert not used_detail
