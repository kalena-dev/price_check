"""Persistent Discord bot for interactive, on-demand value rankings.

Run this process on an always-on host. GitHub Actions cron jobs cannot receive
Discord interactions because they exit after each watcher cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import discord
import yaml
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from notifier.discord import COLOR_RANKED, build_ranking_embeds
from ranking import RankedDeal, rank_listings
from store.sqlite import Store


load_dotenv()
ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", ROOT / "config.yaml"))
DB_PATH = Path(os.getenv("STATE_DB_PATH", ROOT / "state.db"))
logger = logging.getLogger("discord_bot")

CATEGORY_CUSTOM_ID = "deals:category"
PREVIOUS_CUSTOM_ID = "deals:previous"
NEXT_CUSTOM_ID = "deals:next"
REFRESH_CUSTOM_ID = "deals:refresh"
STATE_PREFIX = "deal-menu:"
_STATE_RE = re.compile(r"deal-menu:(laptop|prebuilt):(\d+)/(\d+)")


def _ranking_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    return config.get("ranking") or {}


def _current_ranking(product_type: str) -> list[RankedDeal]:
    config = _ranking_config()
    max_price = min(
        Decimal("3000"),
        Decimal(str(config.get("max_price_cad", 3000))),
    )
    max_age = float(config.get("max_age_hours", 36))
    key = "laptop_limit" if product_type == "laptop" else "prebuilt_limit"
    limit = min(10, int(config.get(key, 10)))
    with Store(DB_PATH) as store:
        current = store.current_listings(
            product_type,
            max_age_hours=max_age,
            max_price_cad=max_price,
        )
    return rank_listings(
        current,
        product_type=product_type,
        max_price_cad=max_price,
        limit=limit,
    )


def _daily_ranking_due(day: str) -> bool:
    """Read scheduler state without claiming the day."""
    with Store(DB_PATH) as store:
        return store.daily_ranking_due(day)


def _claim_daily_ranking(day: str) -> bool:
    with Store(DB_PATH) as store:
        return store.claim_daily_ranking(day)


def _category_label(product_type: str, *, plural: bool = False) -> str:
    if product_type == "laptop":
        return "Laptops" if plural else "Laptop"
    return "Prebuilts" if plural else "Prebuilt"


def _state_marker(product_type: str, page: int, total: int) -> str:
    shown_page = page + 1 if total else 0
    return f"{STATE_PREFIX}{product_type}:{shown_page}/{total}"


def _parse_menu_state(embed: Any) -> tuple[str, int, int]:
    """Return ``(category, zero-based page, total)`` from an embed footer."""
    footer = getattr(embed, "footer", None)
    text = getattr(footer, "text", None)
    if text is None and isinstance(embed, dict):
        text = (embed.get("footer") or {}).get("text")
    match = _STATE_RE.search(text or "")
    if not match:
        return "laptop", 0, 0

    product_type, shown_page, total = match.groups()
    total_int = int(total)
    if total_int <= 0:
        return product_type, 0, 0
    page = min(max(int(shown_page) - 1, 0), total_int - 1)
    return product_type, page, total_int


def _interaction_state(interaction: discord.Interaction) -> tuple[str, int, int]:
    message = getattr(interaction, "message", None)
    embeds = getattr(message, "embeds", None) or []
    if not embeds:
        return "laptop", 0, 0
    return _parse_menu_state(embeds[0])


def _build_menu(
    ranked: list[RankedDeal],
    product_type: str,
    page: int = 0,
) -> tuple[discord.Embed, "DealCarouselView"]:
    """Build one best-first deal page and controls for the current catalog."""
    ranked = ranked[:10]
    total = len(ranked)
    page = min(max(page, 0), total - 1) if total else 0

    if total:
        # The notifier's historical stack is worst-first. Reversing it restores
        # the ranking module's best-first order for one-at-a-time navigation.
        payloads = list(reversed(build_ranking_embeds(ranked, product_type)))
        embed = discord.Embed.from_dict(payloads[page])
        existing_footer = embed.footer.text or "Top deals"
        embed.set_footer(
            text=f"{existing_footer} · {_state_marker(product_type, page, total)}"
        )
        product_url = ranked[page].listing.url
    else:
        label = _category_label(product_type, plural=True).lower()
        embed = discord.Embed(
            title=f"No current {_category_label(product_type).lower()} deals",
            description=(
                f"No {label} under $3,000 were seen in the configured freshness "
                "window. Try the other category or refresh after the next scan."
            ),
            color=COLOR_RANKED,
        )
        embed.set_footer(text=_state_marker(product_type, 0, 0))
        product_url = None

    view = DealCarouselView(
        product_type=product_type,
        page=page,
        total=total,
        product_url=product_url,
    )
    return embed, view


async def _send_interaction_error(
    interaction: discord.Interaction,
    product_type: str,
    exc: Exception,
) -> None:
    logger.exception("failed to build %s ranking", product_type, exc_info=exc)
    try:
        await interaction.followup.send(
            f"Could not refresh the ranking: `{str(exc)[:500]}`",
            ephemeral=True,
        )
    except Exception:
        logger.exception("failed to send interaction error")


async def _update_menu(
    interaction: discord.Interaction,
    product_type: str,
    page: int,
) -> None:
    """Refresh one public menu in place after acknowledging its interaction."""
    await interaction.response.defer()
    try:
        ranked = await asyncio.to_thread(_current_ranking, product_type)
        embed, view = _build_menu(ranked, product_type, page)
        await interaction.edit_original_response(embed=embed, view=view)
    except Exception as exc:
        # The existing message remains usable if either the catalog read or the
        # replacement fails. Only the user who clicked receives the error.
        await _send_interaction_error(interaction, product_type, exc)


class DealCarouselView(discord.ui.View):
    """Persistent, stateless controls for a shared deal carousel.

    Callback state comes from the message footer rather than this Python
    instance, allowing registered persistent controls to serve old messages
    after the bot restarts.
    """

    def __init__(
        self,
        *,
        product_type: str = "laptop",
        page: int = 0,
        total: int = 0,
        product_url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        for option in self.category.options:
            option.default = option.value == product_type
        self.previous.disabled = total == 0 or page <= 0
        self.next.disabled = total == 0 or page >= total - 1
        if product_url:
            self.add_item(discord.ui.Button(
                label="View product",
                style=discord.ButtonStyle.link,
                url=product_url,
                row=1,
            ))

    @discord.ui.select(
        placeholder="Choose deal category",
        custom_id=CATEGORY_CUSTOM_ID,
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Laptops", value="laptop"),
            discord.SelectOption(label="Prebuilt PCs", value="prebuilt"),
        ],
        row=0,
    )
    async def category(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        await _update_menu(interaction, select.values[0], 0)

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        custom_id=PREVIOUS_CUSTOM_ID,
        row=1,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        product_type, page, _ = _interaction_state(interaction)
        await _update_menu(interaction, product_type, page - 1)

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.primary,
        custom_id=NEXT_CUSTOM_ID,
        row=1,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        product_type, page, _ = _interaction_state(interaction)
        await _update_menu(interaction, product_type, page + 1)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id=REFRESH_CUSTOM_ID,
        row=1,
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        product_type, page, _ = _interaction_state(interaction)
        await _update_menu(interaction, product_type, page)


class DealBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._daily_channel_id: int | None = None

    async def setup_hook(self) -> None:
        # Register before connecting so custom IDs on previously sent menus are
        # dispatched to this stateless view after a restart.
        self.add_view(DealCarouselView())

        ranking_config = await asyncio.to_thread(_ranking_config)
        channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if (
            channel_id
            and ranking_config.get("post_daily", True)
            and str(ranking_config.get("presentation", "menu")).lower() == "menu"
        ):
            try:
                self._daily_channel_id = int(channel_id)
            except ValueError:
                logger.warning("DISCORD_CHANNEL_ID is not a valid integer")
            else:
                if not self.daily_menu.is_running():
                    self.daily_menu.start()

        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            commands = await self.tree.sync(guild=guild)
            logger.info("synced %d commands to guild %s", len(commands), guild_id)
        else:
            commands = await self.tree.sync()
            logger.info("synced %d global commands", len(commands))

    async def on_ready(self) -> None:
        logger.info("logged in as %s", self.user)

    async def close(self) -> None:
        if self.daily_menu.is_running():
            self.daily_menu.cancel()
        await super().close()

    @tasks.loop(minutes=30)
    async def daily_menu(self) -> None:
        """Post at most one interactive menu per UTC day."""
        if self._daily_channel_id is None:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        try:
            if not await asyncio.to_thread(_daily_ranking_due, day):
                return
            laptop_ranked, prebuilt_ranked = await asyncio.gather(
                asyncio.to_thread(_current_ranking, "laptop"),
                asyncio.to_thread(_current_ranking, "prebuilt"),
            )
            if not laptop_ranked and not prebuilt_ranked:
                return

            channel = self.get_channel(self._daily_channel_id)
            if channel is None:
                channel = await self.fetch_channel(self._daily_channel_id)
            initial_type = "laptop" if laptop_ranked else "prebuilt"
            initial_ranking = laptop_ranked or prebuilt_ranked
            embed, view = _build_menu(initial_ranking, initial_type, 0)
            await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
            # Claim only after Discord accepted the message, so transient send
            # failures can be retried by the next loop iteration.
            await asyncio.to_thread(_claim_daily_ranking, day)
        except Exception:
            logger.exception("failed to post daily deal menu")

    @daily_menu.before_loop
    async def before_daily_menu(self) -> None:
        await self.wait_until_ready()


bot = DealBot()


async def _send_ranking(interaction: discord.Interaction, product_type: str) -> None:
    await interaction.response.defer(thinking=True)
    try:
        ranked = await asyncio.to_thread(_current_ranking, product_type)
        embed, view = _build_menu(ranked, product_type, 0)
        await interaction.followup.send(embed=embed, view=view)
    except Exception as exc:
        await _send_interaction_error(interaction, product_type, exc)


@bot.tree.command(name="deals", description="Browse the current best laptop and prebuilt deals")
async def deals(interaction: discord.Interaction) -> None:
    await _send_ranking(interaction, "laptop")


@bot.tree.command(name="laptops", description="Browse the current top laptop values")
async def laptops(interaction: discord.Interaction) -> None:
    await _send_ranking(interaction, "laptop")


@bot.tree.command(name="prebuilts", description="Browse the current top prebuilt PC values")
async def prebuilts(interaction: discord.Interaction) -> None:
    await _send_ranking(interaction, "prebuilt")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
