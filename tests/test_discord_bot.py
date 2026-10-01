"""Tests for the persistent, one-message Discord deal carousel."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

import discord_bot
from discord_bot import (
    CATEGORY_CUSTOM_ID,
    NEXT_CUSTOM_ID,
    PREVIOUS_CUSTOM_ID,
    REFRESH_CUSTOM_ID,
    DealCarouselView,
    _build_menu,
    _parse_menu_state,
    _send_ranking,
)
from ranking import RankedDeal
from retailers.base import Listing


def _ranked(product_type: str, *prices: str) -> list[RankedDeal]:
    deals = []
    for index, price in enumerate(prices, start=1):
        listing = Listing(
            retailer="example",
            sku=f"{product_type}-{index}",
            url=f"https://example.com/{product_type}/{index}",
            title=f"{product_type.title()} deal {index}",
            cpu="8845HS" if product_type == "laptop" else "7700X",
            ram_gb=32,
            gpu="RTX 4060" if product_type == "laptop" else "RTX 5070",
            price_cad=Decimal(price),
            image_url=None,
            product_type=product_type,
            availability="in_stock",
        )
        deals.append(RankedDeal(
            listing=listing,
            fair_value_cad=Decimal("2500"),
            value_index=Decimal(200 - index),
            confidence="high",
        ))
    return deals


def _interaction(embed: discord.Embed | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(embeds=[] if embed is None else [embed]),
    )


def _button(view: DealCarouselView, custom_id: str) -> discord.ui.Button:
    return next(child for child in view.children if child.custom_id == custom_id)


def _product_link(view: DealCarouselView) -> discord.ui.Button:
    return next(
        child
        for child in view.children
        if getattr(child, "style", None) is discord.ButtonStyle.link
    )


def test_view_has_persistent_explicit_controls_and_boundary_states() -> None:
    view = DealCarouselView(
        product_type="prebuilt",
        page=1,
        total=3,
        product_url="https://example.com/deal",
    )

    assert view.timeout is None
    assert view.is_persistent()
    assert view.category.custom_id == CATEGORY_CUSTOM_ID
    assert {child.custom_id for child in view.children if child.custom_id} == {
        CATEGORY_CUSTOM_ID,
        PREVIOUS_CUSTOM_ID,
        NEXT_CUSTOM_ID,
        REFRESH_CUSTOM_ID,
    }
    assert [option.default for option in view.category.options] == [False, True]
    assert not view.previous.disabled
    assert not view.next.disabled
    assert _product_link(view).url == "https://example.com/deal"
    assert not _product_link(view).disabled

    first = DealCarouselView(product_type="laptop", page=0, total=3)
    last = DealCarouselView(product_type="laptop", page=2, total=3)
    assert first.previous.disabled and not first.next.disabled
    assert not last.previous.disabled and last.next.disabled


def test_menu_is_one_embed_in_best_first_rank_order() -> None:
    ranked = _ranked("prebuilt", "1700", "2000", "2300")

    first_embed, first_view = _build_menu(ranked, "prebuilt", 0)
    second_embed, second_view = _build_menu(ranked, "prebuilt", 1)

    assert first_embed.title.startswith("#1 Prebuilt value")
    assert second_embed.title.startswith("#2 Prebuilt value")
    assert first_embed.url == ranked[0].listing.url
    assert _parse_menu_state(first_embed) == ("prebuilt", 0, 3)
    assert _parse_menu_state(second_embed) == ("prebuilt", 1, 3)
    assert first_view.previous.disabled
    assert not first_view.next.disabled
    assert not second_view.previous.disabled


@pytest.mark.parametrize("product_type", ["laptop", "prebuilt"])
def test_empty_category_still_has_switch_refresh_and_navigation_controls(
    product_type: str,
) -> None:
    embed, view = _build_menu([], product_type, 20)

    assert embed.title == f"No current {product_type} deals"
    assert _parse_menu_state(embed) == (product_type, 0, 0)
    assert view.category.disabled is False
    assert view.refresh.disabled is False
    assert view.previous.disabled and view.next.disabled
    assert all(
        getattr(child, "style", None) is not discord.ButtonStyle.link
        for child in view.children
    )


def test_persistent_next_reconstructs_category_and_page_from_footer() -> None:
    async def exercise() -> None:
        ranked = _ranked("prebuilt", "1700", "2000", "2300")
        source_embed, _ = _build_menu(ranked, "prebuilt", 0)
        interaction = _interaction(source_embed)
        # This default laptop view represents the stateless view registered
        # after restart; the old footer must override its constructor state.
        persistent_view = DealCarouselView()

        with patch("discord_bot._current_ranking", return_value=ranked) as current:
            await persistent_view.next.callback(interaction)

        interaction.response.defer.assert_awaited_once_with()
        current.assert_called_once_with("prebuilt")
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["embed"].title.startswith("#2 Prebuilt value")
        assert _parse_menu_state(kwargs["embed"]) == ("prebuilt", 1, 3)
        assert not kwargs["view"].previous.disabled

    asyncio.run(exercise())


def test_category_switch_resets_to_first_page() -> None:
    async def exercise() -> None:
        prebuilts = _ranked("prebuilt", "1700", "2000", "2300")
        laptops = _ranked("laptop", "900", "1200")
        source_embed, _ = _build_menu(prebuilts, "prebuilt", 2)
        interaction = _interaction(source_embed)
        persistent_view = DealCarouselView()
        persistent_view.category._values = ["laptop"]

        with patch("discord_bot._current_ranking", return_value=laptops) as current:
            await persistent_view.category.callback(interaction)

        current.assert_called_once_with("laptop")
        kwargs = interaction.edit_original_response.await_args.kwargs
        assert kwargs["embed"].title.startswith("#1 Laptop value")
        assert _parse_menu_state(kwargs["embed"]) == ("laptop", 0, 2)
        assert kwargs["view"].previous.disabled

    asyncio.run(exercise())


def test_refresh_failure_is_ephemeral_and_preserves_existing_menu() -> None:
    async def exercise() -> None:
        ranked = _ranked("laptop", "900", "1200")
        source_embed, _ = _build_menu(ranked, "laptop", 1)
        interaction = _interaction(source_embed)
        persistent_view = DealCarouselView()

        with patch(
            "discord_bot._current_ranking",
            side_effect=RuntimeError("database busy"),
        ):
            await persistent_view.refresh.callback(interaction)

        interaction.response.defer.assert_awaited_once_with()
        interaction.edit_original_response.assert_not_awaited()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "database busy" in interaction.followup.send.await_args.args[0]

    asyncio.run(exercise())


def test_daily_menu_posts_when_only_prebuilts_exist_then_claims_day() -> None:
    async def exercise() -> None:
        prebuilts = _ranked("prebuilt", "1700")
        channel = SimpleNamespace(send=AsyncMock())
        client = discord_bot.DealBot()
        client._daily_channel_id = 123
        events: list[str] = []
        channel.send.side_effect = lambda **_kwargs: events.append("send")

        def claim(_day: str) -> bool:
            events.append("claim")
            return True

        def current(product_type: str):
            return [] if product_type == "laptop" else prebuilts

        with (
            patch("discord_bot._daily_ranking_due", return_value=True),
            patch("discord_bot._current_ranking", side_effect=current),
            patch("discord_bot._claim_daily_ranking", side_effect=claim),
            patch.object(client, "get_channel", return_value=channel),
        ):
            await client.daily_menu.coro(client)

        assert events == ["send", "claim"]
        kwargs = channel.send.await_args.kwargs
        # Avoid leading with an empty card when prebuilts are the only deals.
        assert _parse_menu_state(kwargs["embed"]) == ("prebuilt", 0, 1)
        assert kwargs["embed"].title.startswith("#1 Prebuilt value")
        assert isinstance(kwargs["view"], DealCarouselView)
        assert not kwargs["view"].category.disabled

    asyncio.run(exercise())


def test_daily_menu_does_not_claim_after_failed_send() -> None:
    async def exercise() -> None:
        channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("Discord unavailable")))
        client = discord_bot.DealBot()
        client._daily_channel_id = 123
        with (
            patch("discord_bot._daily_ranking_due", return_value=True),
            patch("discord_bot._current_ranking", return_value=_ranked("laptop", "900")),
            patch("discord_bot._claim_daily_ranking") as claim,
            patch.object(client, "get_channel", return_value=channel),
        ):
            await client.daily_menu.coro(client)
        channel.send.assert_awaited_once()
        claim.assert_not_called()

    asyncio.run(exercise())


def test_daily_menu_skips_already_posted_day() -> None:
    async def exercise() -> None:
        client = discord_bot.DealBot()
        client._daily_channel_id = 123
        with (
            patch("discord_bot._daily_ranking_due", return_value=False),
            patch("discord_bot._current_ranking") as current,
        ):
            await client.daily_menu.coro(client)
        current.assert_not_called()

    asyncio.run(exercise())


def test_slash_ranking_sends_one_public_embed_with_controls() -> None:
    async def exercise() -> None:
        ranked = _ranked("laptop", "900", "1200", "1500")
        interaction = _interaction()

        with patch("discord_bot._current_ranking", return_value=ranked):
            await _send_ranking(interaction, "laptop")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        kwargs = interaction.followup.send.await_args.kwargs
        assert "embed" in kwargs and "embeds" not in kwargs
        assert kwargs["embed"].title.startswith("#1 Laptop value")
        assert isinstance(kwargs["view"], DealCarouselView)
        assert _button(kwargs["view"], NEXT_CUSTOM_ID).disabled is False

    asyncio.run(exercise())
