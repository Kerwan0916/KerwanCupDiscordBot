"""Admin slash commands."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .challonge import ChallongeError, parse_tournament_ref
from .db import OPEN, VERIFIED, GuildConfig
from .service import ConfigError, score_line, validate_scores

if TYPE_CHECKING:
    import asyncpg

    from .bot import KerwanCupBot

log = logging.getLogger(__name__)

ADMIN_PERMS = discord.Permissions(manage_guild=True)


def _chunks(lines: List[str], limit: int = 4000) -> List[str]:
    out: List[str] = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        out.append(buf)
    return out or ["(none)"]


class AdminCommands(commands.Cog):
    tournament = app_commands.Group(
        name="tournament", description="Run the tournament", default_permissions=ADMIN_PERMS, guild_only=True
    )
    team = app_commands.Group(
        name="team", description="Link Challonge teams to Discord roles", default_permissions=ADMIN_PERMS, guild_only=True
    )
    match = app_commands.Group(
        name="match", description="Admin tools for a single match", default_permissions=ADMIN_PERMS, guild_only=True
    )

    def __init__(self, bot: "KerwanCupBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------ plumbing

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.NoPrivateMessage()
        cfg = await self.bot.db.get_config(interaction.guild.id)
        if self.bot.service.is_admin(interaction.user, cfg):
            return True
        raise app_commands.CheckFailure("Only tournament admins can use this command.")

    async def cog_app_command_error(  # type: ignore[override]
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, (ConfigError, ChallongeError, app_commands.CheckFailure)):
            msg = f"❌ {original}"
        else:
            log.exception("Command failed", exc_info=original)
            msg = "❌ Something went wrong. Check the bot's logs."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def _resolve_match(
        self, interaction: discord.Interaction, match_id: Optional[str]
    ) -> "asyncpg.Record":
        if match_id:
            try:
                row = await self.bot.db.get_match(int(match_id))
            except ValueError:
                raise ConfigError("Match ids are numbers (see the footer of the match thread).")
        else:
            row = await self.bot.db.get_match_by_thread(interaction.channel_id or 0)
        if row is None:
            raise ConfigError("Match not found. Run this inside the match thread, or pass `match_id`.")
        return row

    # --------------------------------------------------------------- setup

    @app_commands.command(name="setup", description="Configure the bot for this server")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(
        tournament="Challonge tournament URL or slug, e.g. https://challonge.com/kerwan_cup_2026",
        captain_role="Role every team captain has",
        matches_channel="Channel under which private match threads are created",
        results_channel="Channel where verified results are announced",
        admin_role="Role pinged on disputes and allowed to use admin commands",
        bracket_channel="Where bracket images are posted (default: results channel)",
        alerts_channel="Admin-only channel for problems the bot can't solve itself",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        tournament: str,
        captain_role: discord.Role,
        matches_channel: discord.TextChannel,
        results_channel: discord.TextChannel,
        admin_role: Optional[discord.Role] = None,
        bracket_channel: Optional[discord.TextChannel] = None,
        alerts_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        slug = parse_tournament_ref(tournament)
        info = await self.bot.challonge.get_tournament(slug)

        cfg = GuildConfig(
            guild_id=interaction.guild.id,
            tournament=slug,
            captain_role_id=captain_role.id,
            admin_role_id=admin_role.id if admin_role else None,
            matches_channel_id=matches_channel.id,
            results_channel_id=results_channel.id,
            bracket_channel_id=bracket_channel.id if bracket_channel else None,
            alerts_channel_id=alerts_channel.id if alerts_channel else None,
        )
        await self.bot.db.save_config(cfg)
        teams = await self.bot.service.sync_teams(interaction.guild, cfg)

        embed = discord.Embed(title=f"✅ Linked to {info.get('name')}", url=info.get("full_challonge_url"))
        embed.add_field(name="Format", value=str(info.get("tournament_type", "?")).title())
        embed.add_field(name="State", value=str(info.get("state", "?")).replace("_", " "))
        embed.add_field(name="Teams", value=str(len(teams)))
        problems = [t.problem for t in teams if t.problem]
        if problems:
            embed.add_field(name="⚠️ Needs attention", value=_chunks(problems, 1000)[0], inline=False)
        embed.set_footer(text="Next: start the tournament on Challonge, then run /tournament publish")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------------------------------------- tournament

    @tournament.command(name="publish", description="Open threads for every playable match on Challonge")
    async def publish(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        report = await self.bot.service.refresh(interaction.guild)
        for chunk in _chunks(report.summary().split("\n"), 1900):
            await interaction.followup.send(chunk, ephemeral=True)

    @tournament.command(name="status", description="Show matches that are waiting on reports")
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        cfg = await self.bot.service.require_config(interaction.guild)
        assert cfg.tournament
        rows = await self.bot.db.list_matches(cfg.tournament, [OPEN, VERIFIED])
        lines = []
        for r in rows:
            where = f"<#{r['thread_id']}>" if r["thread_id"] else "(no thread)"
            if r["status"] == VERIFIED:
                state = "⚠️ confirmed but not on Challonge yet (`/match resync`)"
            else:
                a = "✅" if r["sub1_score1"] is not None else "⏳"
                b = "✅" if r["sub2_score1"] is not None else "⏳"
                state = f"{a} {r['team1_name']} · {b} {r['team2_name']}"
                if r["mismatch_count"]:
                    state += f" · {r['mismatch_count']} mismatch(es)"
            lines.append(f"`{r['match_id']}` {where} {state}")
        embed = discord.Embed(
            title=f"Active matches ({len(rows)})",
            description=_chunks(lines)[0] if lines else "No active matches.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tournament.command(name="bracket", description="Post the current bracket image now")
    async def bracket(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        cfg = await self.bot.service.require_config(interaction.guild)
        assert cfg.tournament
        info = await self.bot.challonge.get_tournament(cfg.tournament)
        await self.bot.service.post_bracket(interaction.guild, cfg, info, "📊 Current bracket")
        await interaction.followup.send("Bracket posted.", ephemeral=True)

    # ---------------------------------------------------------------- team

    @team.command(name="list", description="Show each Challonge team, its role and its captains")
    async def team_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        cfg = await self.bot.service.require_config(interaction.guild)
        teams = await self.bot.service.sync_teams(interaction.guild, cfg)
        lines = []
        for t in teams:
            role = t.role.mention if t.role else "❌ no role"
            caps = ", ".join(c.mention for c in t.captains) if t.captains else "⚠️ no captain"
            lines.append(f"**{t.name}** → {role} · captain: {caps}")
        embed = discord.Embed(title=f"Teams ({len(teams)})", description=_chunks(lines)[0] if lines else "No teams on Challonge yet.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @team.command(name="link", description="Link a Challonge team to a Discord role (if names don't match)")
    @app_commands.describe(team="Team as named on Challonge", role="The team's Discord role")
    async def team_link(self, interaction: discord.Interaction, team: str, role: discord.Role) -> None:
        assert interaction.guild is not None
        cfg = await self.bot.service.require_config(interaction.guild)
        assert cfg.tournament
        try:
            pid = int(team)
        except ValueError:
            raise ConfigError("Pick the team from the list that appears while typing.")
        if not await self.bot.db.set_team_role(cfg.tournament, pid, role.id):
            raise ConfigError("Unknown team. Run `/team list` first to load teams from Challonge.")
        await interaction.response.send_message(f"Linked to {role.mention}.", ephemeral=True)

    @team_link.autocomplete("team")
    async def team_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        cfg = await self.bot.db.get_config(interaction.guild_id or 0)
        if not cfg.tournament:
            return []
        rows = await self.bot.db.get_teams(cfg.tournament)
        cur = current.casefold()
        return [
            app_commands.Choice(name=r["name"][:100], value=str(r["participant_id"]))
            for r in rows
            if cur in r["name"].casefold()
        ][:25]

    # --------------------------------------------------------------- match

    @match.command(name="force", description="Set a match result as admin (overrides captains)")
    @app_commands.describe(
        team1_score="Score of the FIRST team in the match (left side)",
        team2_score="Score of the SECOND team in the match (right side)",
        match_id="Only needed when not run inside the match thread",
    )
    async def match_force(
        self,
        interaction: discord.Interaction,
        team1_score: app_commands.Range[int, 0, 99],
        team2_score: app_commands.Range[int, 0, 99],
        match_id: Optional[str] = None,
    ) -> None:
        assert interaction.guild is not None
        row = await self._resolve_match(interaction, match_id)
        error = validate_scores(team1_score, team2_score, row["allow_ties"])
        if error:
            raise ConfigError(error)
        if not await self.bot.db.force_result(row["match_id"], team1_score, team2_score, interaction.user.id):
            raise ConfigError("That match is already completed.")
        await interaction.response.send_message(
            f"Setting {score_line(row, team1_score, team2_score)}…", ephemeral=True
        )
        ok = await self.bot.service.finalize_match(interaction.guild, row["match_id"])
        if not ok:
            await interaction.followup.send(
                "Saved, but Challonge couldn't be updated. Retry with `/match resync`.", ephemeral=True
            )

    @match.command(name="reset", description="Clear both captains' reports so they can report again")
    @app_commands.describe(match_id="Only needed when not run inside the match thread")
    async def match_reset(self, interaction: discord.Interaction, match_id: Optional[str] = None) -> None:
        assert interaction.guild is not None
        row = await self._resolve_match(interaction, match_id)
        if not await self.bot.db.reset_submissions(row["match_id"]):
            raise ConfigError("Only open matches can be reset.")
        await interaction.response.send_message("Reports cleared.", ephemeral=True)
        thread = interaction.guild.get_thread(row["thread_id"] or 0)
        if thread is not None:
            await thread.send("🔄 An admin cleared both reports. Captains, please report again.")

    @match.command(name="resync", description="Retry sending a confirmed result to Challonge")
    @app_commands.describe(match_id="Only needed when not run inside the match thread")
    async def match_resync(self, interaction: discord.Interaction, match_id: Optional[str] = None) -> None:
        assert interaction.guild is not None
        row = await self._resolve_match(interaction, match_id)
        if row["status"] != VERIFIED:
            raise ConfigError("Only confirmed results that failed to reach Challonge can be resynced.")
        await interaction.response.send_message("Retrying…", ephemeral=True)
        ok = await self.bot.service.finalize_match(interaction.guild, row["match_id"])
        await interaction.followup.send("✅ Done." if ok else "❌ Still failing; see the alert.", ephemeral=True)
