"""Persistent "Report score" button and the blind score modal."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import ui

from .service import score_line, validate_scores

if TYPE_CHECKING:
    from .bot import KerwanCupBot

log = logging.getLogger(__name__)


def _label(name: str) -> str:
    suffix = " score"
    return (name[: 45 - len(suffix)] + suffix) if len(name) + len(suffix) > 45 else name + suffix


def report_view(match_id: int) -> ui.View:
    view = ui.View(timeout=None)
    view.add_item(ReportButton(match_id))
    return view


class ReportButton(ui.DynamicItem[ui.Button], template=r"kc:report:(?P<match_id>[0-9]+)"):
    """Its custom_id carries the match id, so it keeps working after the bot restarts."""

    def __init__(self, match_id: int) -> None:
        super().__init__(
            ui.Button(
                label="Report score",
                emoji="📝",
                style=discord.ButtonStyle.primary,
                custom_id=f"kc:report:{match_id}",
            )
        )
        self.match_id = match_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match: re.Match):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        bot: "KerwanCupBot" = interaction.client  # type: ignore[assignment]
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        match = await bot.db.get_match(self.match_id)
        if match is None or match["status"] != "open":
            await interaction.response.send_message("This match is no longer accepting reports.", ephemeral=True)
            return
        cfg = await bot.db.get_config(interaction.guild.id)
        side, error = bot.service.captain_side(interaction.user, match, cfg)
        if side is None:
            await interaction.response.send_message(error or "You can't report this match.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ScoreModal(self.match_id, side, match["team1_name"] or "Team 1", match["team2_name"] or "Team 2")
        )


class ScoreModal(ui.Modal):
    def __init__(self, match_id: int, side: int, team1: str, team2: str) -> None:
        super().__init__(title="Report series score", timeout=600)
        self.match_id = match_id
        self.side = side
        self.team1 = team1
        self.team2 = team2
        self.score1: ui.TextInput = ui.TextInput(label=_label(team1), placeholder="e.g. 2", min_length=1, max_length=2)
        self.score2: ui.TextInput = ui.TextInput(label=_label(team2), placeholder="e.g. 1", min_length=1, max_length=2)
        self.add_item(self.score1)
        self.add_item(self.score2)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        bot: "KerwanCupBot" = interaction.client  # type: ignore[assignment]
        try:
            s1, s2 = int(self.score1.value.strip()), int(self.score2.value.strip())
        except ValueError:
            await interaction.response.send_message("Scores must be whole numbers, e.g. `2` and `1`.", ephemeral=True)
            return

        match = await bot.db.get_match(self.match_id)
        if match is None:
            await interaction.response.send_message("This match no longer exists.", ephemeral=True)
            return
        error = validate_scores(s1, s2, match["allow_ties"])
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        result = await bot.db.submit_score(self.match_id, self.side, interaction.user.id, s1, s2)
        row = result.match
        if result.outcome == "closed" or row is None:
            await interaction.response.send_message("This match is no longer accepting reports.", ephemeral=True)
            return

        mine = row["team1_name"] if self.side == 1 else row["team2_name"]
        other = row["team2_name"] if self.side == 1 else row["team1_name"]

        if result.outcome in ("recorded", "updated"):
            await interaction.response.send_message(
                f"🔒 Saved your report: {score_line(row, s1, s2)}.\n"
                f"It stays hidden from **{other}**. You can submit again to change it until they report.",
                ephemeral=True,
            )
            if result.outcome == "recorded" and isinstance(interaction.channel, discord.abc.Messageable):
                await interaction.channel.send(f"📝 **{mine}** has reported a result. Waiting for **{other}**…")
            return

        if result.outcome == "mismatch":
            o1, o2 = result.other_report or (None, None)
            cfg = await bot.db.get_config(interaction.guild_id or 0)
            ping = f"<@&{cfg.admin_role_id}>" if cfg.admin_role_id else "An admin"
            await interaction.response.send_message(
                "⚠️ **The reports don't match**, so both have been cleared.\n"
                f"• **{mine}** reported {score_line(row, s1, s2)}\n"
                f"• **{other}** reported {score_line(row, o1, o2)}\n"
                f"Please agree on the score and both report again. {ping} has been notified.",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            return

        # verified
        await interaction.response.send_message(
            f"✅ Both captains reported {score_line(row, s1, s2)}. Updating Challonge and posting the result…"
        )
        if interaction.guild is not None:
            await bot.service.finalize_match(interaction.guild, self.match_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        log.exception("Score modal failed", exc_info=error)
        msg = "Something went wrong saving your report. Please try again, or ask an admin."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
