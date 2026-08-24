"""Persistent Discord slash-command bot for on-demand value rankings.

Run this process on an always-on host. GitHub Actions cron jobs cannot receive
Discord interactions because they exit after each watcher cycle.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path

import discord
import yaml
from discord import app_commands
from dotenv import load_dotenv

from notifier.discord import build_ranking_embeds
from ranking import rank_listings
from store.sqlite import Store


load_dotenv()
ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", ROOT / "config.yaml"))
DB_PATH = Path(os.getenv("STATE_DB_PATH", ROOT / "state.db"))
logger = logging.getLogger("discord_bot")


def _ranking_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    return config.get("ranking") or {}


def _current_ranking(product_type: str):
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


class DealBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
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


bot = DealBot()


async def _send_ranking(interaction: discord.Interaction, product_type: str) -> None:
    await interaction.response.defer(thinking=True)
    try:
        ranked = _current_ranking(product_type)
    except Exception as exc:
        logger.exception("failed to build %s ranking", product_type)
        await interaction.followup.send(
            f"Could not build the ranking: `{str(exc)[:500]}`",
            ephemeral=True,
        )
        return

    if not ranked:
        label = "laptops" if product_type == "laptop" else "prebuilts"
        await interaction.followup.send(
            f"No current {label} under $3,000 were seen in the freshness window."
        )
        return

    payloads = build_ranking_embeds(ranked, product_type)
    embeds = [discord.Embed.from_dict(payload) for payload in payloads]
    await interaction.followup.send(embeds=embeds)


@bot.tree.command(name="laptops", description="Print the current top 10 laptop values")
async def laptops(interaction: discord.Interaction) -> None:
    await _send_ranking(interaction, "laptop")


@bot.tree.command(name="prebuilts", description="Print the current top 10 prebuilt PC values")
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
