from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .challonge import ChallongeClient
from .commands import AdminCommands
from .config import Settings
from .db import Database
from .service import TournamentService
from .views import ReportButton

log = logging.getLogger(__name__)


class KerwanCupBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True  # needed to find who holds the team + captain roles
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.settings = settings
        self.db = Database(settings.database_url)
        self.challonge = ChallongeClient(settings.challonge_username, settings.challonge_api_key)
        self.service = TournamentService(self, self.db, self.challonge)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.db.init_schema()
        self.add_dynamic_items(ReportButton)

        guild = discord.Object(id=self.settings.guild_id)
        await self.add_cog(AdminCommands(self), guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d slash commands to guild %s", len(synced), self.settings.guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        if self.get_guild(self.settings.guild_id) is None:
            log.warning("The bot is not in guild %s yet. Invite it with the URL from the README.", self.settings.guild_id)

    async def close(self) -> None:
        await super().close()
        await self.challonge.close()
        await self.db.close()
