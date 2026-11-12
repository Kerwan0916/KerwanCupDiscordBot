"""Tournament workflow: Challonge sync, match threads, results and bracket posts."""
from __future__ import annotations

import asyncio
import io
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import discord

from .bracket import bracket_svg_url, render_bracket_png
from .challonge import ChallongeClient, ChallongeError
from .db import COMPLETED, OPEN, VERIFIED, Database, GuildConfig

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger(__name__)

TIE_FORMATS = {"round robin", "swiss"}
FINISHED_STATES = {"awaiting_review", "complete"}


class ConfigError(Exception):
    """User-facing problem with the bot's configuration."""


def round_label(round_no: int, group_id: Optional[int], tournament_type: Optional[str] = None) -> str:
    if group_id:
        return f"Group stage · Round {round_no}"
    if round_no < 0:
        return f"Losers round {-round_no}"
    if tournament_type == "double elimination":
        return f"Winners round {round_no}"
    return f"Round {round_no}"


def stage_key(match: dict) -> str:
    return f"{'g' if match.get('group_id') else 'f'}:{match['round']}"


def score_line(row: "asyncpg.Record", s1: int, s2: int) -> str:
    return f"**{row['team1_name']}** {s1} – {s2} **{row['team2_name']}**"


def validate_scores(s1: int, s2: int, allow_ties: bool) -> Optional[str]:
    if s1 < 0 or s2 < 0:
        return "Scores can't be negative."
    if s1 == 0 and s2 == 0:
        return "At least one game must have been played (0–0 isn't a valid result)."
    if s1 == s2 and not allow_ties:
        return "This match can't end in a tie. Please enter the series score (e.g. 2–1)."
    return None


@dataclass
class TeamStatus:
    participant_id: int
    name: str
    role: Optional[discord.Role]
    captains: List[discord.Member]

    @property
    def problem(self) -> Optional[str]:
        if self.role is None:
            return f"**{self.name}** isn't linked to a Discord role (create a role with the same name, or use `/team link`)"
        if not self.captains:
            return f"**{self.name}** has no member with both its team role and the captain role"
        return None


@dataclass
class RefreshReport:
    created: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    stages_posted: List[str] = field(default_factory=list)
    closed_externally: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts: List[str] = []
        if self.created:
            parts.append("**New match threads:**\n" + "\n".join(f"• {c}" for c in self.created))
        if self.stages_posted:
            parts.append("**Bracket posted for:** " + ", ".join(self.stages_posted))
        if self.closed_externally:
            parts.append(
                "**Closed (result entered on Challonge):**\n" + "\n".join(f"• {c}" for c in self.closed_externally)
            )
        if self.problems:
            parts.append("**Needs attention:**\n" + "\n".join(f"• {p}" for p in self.problems))
        return "\n\n".join(parts) or "Nothing new: no open matches waiting for a thread."


class TournamentService:
    def __init__(self, client: discord.Client, db: Database, challonge: ChallongeClient) -> None:
        self.client = client
        self.db = db
        self.challonge = challonge
        self._locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------- helpers

    async def require_config(self, guild: discord.Guild) -> GuildConfig:
        cfg = await self.db.get_config(guild.id)
        missing = cfg.missing()
        if missing:
            raise ConfigError("The bot isn't set up yet (missing " + ", ".join(missing) + "). Run `/setup` first.")
        return cfg

    @staticmethod
    def is_admin(member: discord.Member, cfg: GuildConfig) -> bool:
        if member.guild_permissions.manage_guild:
            return True
        return bool(cfg.admin_role_id and member.get_role(cfg.admin_role_id))

    @staticmethod
    def captain_side(
        member: discord.Member, match: "asyncpg.Record", cfg: GuildConfig
    ) -> Tuple[Optional[int], Optional[str]]:
        """Which side (1 or 2) this member reports for, or an error message."""
        role_ids = {r.id for r in member.roles}
        if cfg.captain_role_id not in role_ids:
            return None, "Only team captains can report scores."
        on1 = match["team1_role_id"] in role_ids
        on2 = match["team2_role_id"] in role_ids
        if on1 and on2:
            return None, "You have both teams' roles, so the bot can't tell which team you're reporting for."
        if on1:
            return 1, None
        if on2:
            return 2, None
        return None, "You're not a captain of either team in this match."

    @staticmethod
    def captains_of(role: Optional[discord.Role], captain_role: Optional[discord.Role]) -> List[discord.Member]:
        if role is None or captain_role is None:
            return []
        return [m for m in role.members if captain_role in m.roles and not m.bot]

    async def _get_thread(self, guild: discord.Guild, thread_id: Optional[int]) -> Optional[discord.Thread]:
        if not thread_id:
            return None
        thread = guild.get_thread(thread_id)
        if thread is not None:
            return thread
        try:
            channel = await self.client.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        return channel if isinstance(channel, discord.Thread) else None

    async def _delete_thread(self, guild: discord.Guild, thread_id: Optional[int]) -> None:
        thread = await self._get_thread(guild, thread_id)
        if thread is None:
            return
        try:
            await thread.delete(reason="Kerwan Cup: match result verified")
        except discord.HTTPException:
            log.exception("Couldn't delete thread %s", thread_id)

    async def alert(self, guild: discord.Guild, cfg: GuildConfig, text: str) -> None:
        """Send an admin alert to the alerts channel (if configured), pinging the admin role."""
        log.warning("Alert for guild %s: %s", guild.id, text)
        if not cfg.alerts_channel_id:
            return
        channel = guild.get_channel(cfg.alerts_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        ping = f"<@&{cfg.admin_role_id}> " if cfg.admin_role_id else ""
        try:
            await channel.send(
                (ping + text)[:2000], allowed_mentions=discord.AllowedMentions(roles=True, users=False)
            )
        except discord.HTTPException:
            log.exception("Couldn't send alert")

    # --------------------------------------------------------------- teams

    async def sync_teams(self, guild: discord.Guild, cfg: GuildConfig) -> List[TeamStatus]:
        """Pull participants from Challonge and link each to a Discord role.

        A team is auto-linked to the role whose name matches its Challonge name
        (case-insensitive). Once linked, the role id is stored, so renaming the role
        later doesn't break anything. `/team link` overrides the automatic match.
        """
        assert cfg.tournament
        participants = await self.challonge.get_participants(cfg.tournament)
        existing = {row["participant_id"]: row for row in await self.db.get_teams(cfg.tournament)}
        roles_by_name: Dict[str, discord.Role] = {}
        for role in guild.roles:
            roles_by_name.setdefault(role.name.strip().casefold(), role)
        captain_role = guild.get_role(cfg.captain_role_id) if cfg.captain_role_id else None

        statuses: List[TeamStatus] = []
        for p in participants:
            pid = int(p["id"])
            name = (p.get("name") or p.get("display_name") or p.get("username") or f"Team {pid}").strip()
            group_ids = [int(x) for x in (p.get("group_player_ids") or [])]
            await self.db.upsert_team(cfg.tournament, pid, name, group_ids)

            row = existing.get(pid)
            role = guild.get_role(row["role_id"]) if row and row["role_id"] else None
            if role is None:
                role = roles_by_name.get(name.casefold())
                if role is not None:
                    await self.db.set_team_role(cfg.tournament, pid, role.id)
            statuses.append(TeamStatus(pid, name, role, self.captains_of(role, captain_role)))
        return statuses

    # ------------------------------------------------------------- refresh

    async def refresh(self, guild: discord.Guild) -> RefreshReport:
        """Sync with Challonge: close externally-finished matches, post completed-round
        brackets, and open threads for newly playable matches."""
        async with self._locks[guild.id]:
            return await self._refresh(guild)

    async def _refresh(self, guild: discord.Guild) -> RefreshReport:
        cfg = await self.require_config(guild)
        assert cfg.tournament
        report = RefreshReport()

        tournament = await self.challonge.get_tournament(cfg.tournament)
        ttype = tournament.get("tournament_type")
        if tournament.get("state") == "pending":
            report.problems.append("The tournament hasn't been started on Challonge yet.")

        teams = await self.sync_teams(guild, cfg)
        by_pid = {t.participant_id: t for t in teams}
        id_map: Dict[int, int] = {}
        for row in await self.db.get_teams(cfg.tournament):
            id_map[row["participant_id"]] = row["participant_id"]
            for gid in row["group_player_ids"]:
                id_map[gid] = row["participant_id"]

        matches = await self.challonge.get_matches(cfg.tournament)
        db_rows = {row["match_id"]: row for row in await self.db.list_matches(cfg.tournament)}

        # 1. Matches the bot opened but whose result was entered on Challonge directly.
        for m in matches:
            row = db_rows.get(m["id"])
            if row is None or row["status"] == COMPLETED or m.get("state") != "complete":
                continue
            await self.db.mark_completed(
                row["match_id"],
                source=None if row["status"] == VERIFIED else "challonge",
                challonge_scores=m.get("scores_csv"),
            )
            fresh = await self.db.get_match(row["match_id"])
            if fresh is not None:
                await self._post_result(guild, cfg, fresh, tournament, challonge_match=m)
            await self._delete_thread(guild, row["thread_id"])
            report.closed_externally.append(f"{row['team1_name']} vs {row['team2_name']}")

        # 2. Bracket image whenever a whole round finishes.
        await self._post_completed_stages(guild, cfg, tournament, matches, set(db_rows), report)

        # 3. Threads for newly playable matches.
        for m in sorted(matches, key=lambda x: (x.get("suggested_play_order") or 0, x["id"])):
            if m.get("state") != "open" or not m.get("player1_id") or not m.get("player2_id"):
                continue
            row = db_rows.get(m["id"])
            if row is not None and (row["thread_id"] or row["status"] != OPEN):
                continue
            await self._open_match(guild, cfg, ttype, m, id_map, by_pid, report)

        return report

    async def _post_completed_stages(
        self,
        guild: discord.Guild,
        cfg: GuildConfig,
        tournament: dict,
        matches: List[dict],
        known_ids: set,
        report: RefreshReport,
    ) -> None:
        assert cfg.tournament
        stages: Dict[str, List[dict]] = defaultdict(list)
        for m in matches:
            stages[stage_key(m)].append(m)

        finished = tournament.get("state") in FINISHED_STATES
        for key, stage_matches in stages.items():
            if not all(m.get("state") == "complete" for m in stage_matches):
                continue
            # Only announce rounds the bot took part in (avoids a flood when set up mid-event).
            if not any(m["id"] in known_ids for m in stage_matches):
                continue
            if not await self.db.claim_stage_post(cfg.tournament, key):
                continue
            if finished:
                continue  # the "tournament complete" post below covers it
            first = stage_matches[0]
            label = round_label(first["round"], first.get("group_id"), tournament.get("tournament_type"))
            await self.post_bracket(guild, cfg, tournament, f"✅ {label} complete")
            report.stages_posted.append(label)

        if finished and matches and await self.db.claim_stage_post(cfg.tournament, "complete"):
            await self.post_bracket(guild, cfg, tournament, "🏆 All matches played: final bracket")
            report.stages_posted.append("Final bracket")

    async def _open_match(
        self,
        guild: discord.Guild,
        cfg: GuildConfig,
        ttype: Optional[str],
        m: dict,
        id_map: Dict[int, int],
        by_pid: Dict[int, TeamStatus],
        report: RefreshReport,
    ) -> None:
        assert cfg.tournament
        from .views import report_view  # local import avoids a cycle

        label = round_label(m["round"], m.get("group_id"), ttype)
        pid1, pid2 = id_map.get(int(m["player1_id"])), id_map.get(int(m["player2_id"]))
        team1, team2 = by_pid.get(pid1) if pid1 else None, by_pid.get(pid2) if pid2 else None
        if team1 is None or team2 is None:
            report.problems.append(f"{label}: Challonge match {m['id']} has a participant the bot doesn't know")
            return
        desc = f"{label}: {team1.name} vs {team2.name}"
        problems = [t.problem for t in (team1, team2) if t.problem]
        if problems:
            report.problems.append(f"{desc}: " + "; ".join(problems))
            return

        channel = guild.get_channel(cfg.matches_channel_id) if cfg.matches_channel_id else None
        if not isinstance(channel, discord.TextChannel):
            raise ConfigError("The matches channel is missing or isn't a text channel. Run `/setup` again.")

        allow_ties = bool(m.get("group_id")) or ttype in TIE_FORMATS
        await self.db.insert_match(
            match_id=int(m["id"]),
            tournament=cfg.tournament,
            guild_id=guild.id,
            round_no=int(m["round"]),
            group_id=m.get("group_id"),
            player1_id=int(m["player1_id"]),
            player2_id=int(m["player2_id"]),
            team1_participant_id=team1.participant_id,
            team2_participant_id=team2.participant_id,
            allow_ties=allow_ties,
        )

        thread: Optional[discord.Thread] = None
        try:
            thread = await channel.create_thread(
                name=f"{team1.name} vs {team2.name}"[:100],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=10080,
                reason=f"Kerwan Cup match {m['id']}",
            )
            captains = team1.captains + team2.captains
            for cap in captains:
                await thread.add_user(cap)

            assert team1.role and team2.role
            embed = discord.Embed(
                title=f"⚔️ {label}",
                description=(
                    f"{team1.role.mention} **vs** {team2.role.mention}\n\n"
                    "When the series is over, **each captain** clicks **Report score** and enters the result.\n"
                    "🔒 Reports are blind: neither captain can see the other's entry.\n"
                    "✅ If both reports match, the result is posted in "
                    f"<#{cfg.results_channel_id}>, Challonge is updated, and this thread is deleted.\n"
                    "⚠️ If they don't match, both reports are cleared and an admin is notified."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(name=f"{team1.name} captain(s)", value=" ".join(c.mention for c in team1.captains))
            embed.add_field(name=f"{team2.name} captain(s)", value=" ".join(c.mention for c in team2.captains))
            embed.set_footer(text=f"Challonge match {m.get('identifier') or ''} · id {m['id']}".strip())
            await thread.send(
                content=" ".join(c.mention for c in captains) + " your match is ready!",
                embed=embed,
                view=report_view(int(m["id"])),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
        except discord.HTTPException as exc:
            log.exception("Couldn't create thread for match %s", m["id"])
            report.problems.append(f"{desc}: couldn't create the thread ({exc.text or exc})")
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass
            await self.db.delete_match(int(m["id"]))
            return

        await self.db.set_match_thread(int(m["id"]), thread.id)
        report.created.append(f"{desc} → {thread.mention}")

    # ------------------------------------------------------------ results

    async def finalize_match(self, guild: discord.Guild, match_id: int) -> bool:
        """Push a verified result to Challonge, announce it, delete the thread and
        continue the bracket. Returns False if Challonge couldn't be updated."""
        async with self._locks[guild.id]:
            row = await self.db.get_match(match_id)
            if row is None or row["status"] != VERIFIED:
                return False
            cfg = await self.require_config(guild)
            assert cfg.tournament
            s1, s2 = row["final_score1"], row["final_score2"]
            if s1 > s2:
                winner = row["player1_id"]
            elif s2 > s1:
                winner = row["player2_id"]
            else:
                winner = "tie"

            try:
                await self.challonge.report_match(cfg.tournament, match_id, f"{s1}-{s2}", winner)
            except ChallongeError as exc:
                thread = await self._get_thread(guild, row["thread_id"])
                msg = (
                    f"⚠️ The result {score_line(row, s1, s2)} is confirmed, but Challonge couldn't be updated: "
                    f"{exc}\nAn admin can retry with `/match resync`."
                )
                if thread is not None:
                    ping = f"<@&{cfg.admin_role_id}> " if cfg.admin_role_id else ""
                    await thread.send(ping + msg, allowed_mentions=discord.AllowedMentions(roles=True))
                await self.alert(guild, cfg, msg)
                return False

            await self.db.mark_completed(match_id)
            try:
                tournament = await self.challonge.get_tournament(cfg.tournament)
            except ChallongeError:
                tournament = {}
            await self._post_result(guild, cfg, row, tournament)
            await self._delete_thread(guild, row["thread_id"])

            try:
                report = await self._refresh(guild)
                if report.problems:
                    await self.alert(guild, cfg, "**Couldn't open some matches:**\n" + "\n".join(f"• {p}" for p in report.problems))
            except Exception:  # noqa: BLE001
                log.exception("Refresh after match %s failed", match_id)
                await self.alert(guild, cfg, "Refreshing the bracket after a result failed. Run `/tournament publish`.")
            return True

    async def _post_result(
        self,
        guild: discord.Guild,
        cfg: GuildConfig,
        row: "asyncpg.Record",
        tournament: dict,
        challonge_match: Optional[dict] = None,
    ) -> None:
        channel = guild.get_channel(cfg.results_channel_id) if cfg.results_channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Results channel missing in guild %s", guild.id)
            return

        t1 = f"<@&{row['team1_role_id']}>" if row["team1_role_id"] else f"**{row['team1_name']}**"
        t2 = f"<@&{row['team2_role_id']}>" if row["team2_role_id"] else f"**{row['team2_name']}**"
        s1, s2 = row["final_score1"], row["final_score2"]
        source = row["result_source"]

        if s1 is None or s2 is None:
            # Result came straight from Challonge.
            raw = (challonge_match or {}).get("scores_csv") or row["challonge_scores"] or "?"
            winner_id = (challonge_match or {}).get("winner_id")
            winner_name = None
            if winner_id == row["player1_id"]:
                winner_name = row["team1_name"]
            elif winner_id == row["player2_id"]:
                winner_name = row["team2_name"]
            description = f"{t1} **vs** {t2}\nScore: `{raw}`"
            footer = "Entered on Challonge by an organiser"
        else:
            description = f"{t1} **{s1} – {s2}** {t2}"
            winner_name = row["team1_name"] if s1 > s2 else row["team2_name"] if s2 > s1 else None
            footer = "Confirmed by both captains" if source == "captains" else "Set by a tournament admin"

        embed = discord.Embed(
            title=round_label(row["round"], row["group_id"], tournament.get("tournament_type")),
            description=description,
            color=discord.Color.gold(),
            url=tournament.get("full_challonge_url") or None,
        )
        embed.add_field(name="Winner", value=f"🏆 {winner_name}" if winner_name else "Draw")
        embed.set_footer(text=footer)
        await channel.send(embed=embed)

    # ------------------------------------------------------------- bracket

    async def post_bracket(self, guild: discord.Guild, cfg: GuildConfig, tournament: dict, title: str) -> None:
        channel_id = cfg.bracket_channel_id or cfg.results_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        link = tournament.get("full_challonge_url")
        embed = discord.Embed(title=title, url=link or None, color=discord.Color.orange())
        svg_url = bracket_svg_url(tournament)
        png = await render_bracket_png(svg_url) if svg_url else None
        if png:
            embed.set_image(url="attachment://bracket.png")
            await channel.send(embed=embed, file=discord.File(io.BytesIO(png), filename="bracket.png"))
        else:
            embed.description = f"[View the live bracket on Challonge]({link})" if link else "Bracket unavailable."
            await channel.send(embed=embed)
