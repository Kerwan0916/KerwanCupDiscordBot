"""Postgres access (works with Neon, Supabase, or any Postgres)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

OPEN = "open"
VERIFIED = "verified"  # scores agreed, not yet written to Challonge
COMPLETED = "completed"

CONFIG_FIELDS = (
    "tournament",
    "captain_role_id",
    "admin_role_id",
    "matches_channel_id",
    "results_channel_id",
    "bracket_channel_id",
    "alerts_channel_id",
)

# Match rows always come back joined with both teams' names and roles.
MATCH_SELECT = """
SELECT m.*,
       t1.name AS team1_name, t1.role_id AS team1_role_id,
       t2.name AS team2_name, t2.role_id AS team2_role_id
FROM matches m
LEFT JOIN teams t1 ON t1.tournament = m.tournament AND t1.participant_id = m.team1_participant_id
LEFT JOIN teams t2 ON t2.tournament = m.tournament AND t2.participant_id = m.team2_participant_id
"""


@dataclass
class GuildConfig:
    guild_id: int
    tournament: Optional[str] = None
    captain_role_id: Optional[int] = None
    admin_role_id: Optional[int] = None
    matches_channel_id: Optional[int] = None
    results_channel_id: Optional[int] = None
    bracket_channel_id: Optional[int] = None
    alerts_channel_id: Optional[int] = None

    def missing(self) -> List[str]:
        required = {
            "tournament": "tournament",
            "captain_role_id": "captain role",
            "matches_channel_id": "matches channel",
            "results_channel_id": "results channel",
        }
        return [label for attr, label in required.items() if getattr(self, attr) is None]


@dataclass
class SubmitResult:
    # recorded | updated | verified | mismatch | closed
    outcome: str
    match: Optional[asyncpg.Record]
    other_report: Optional[Tuple[int, int]] = None


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected")
        return self._pool

    async def connect(self) -> None:
        # statement_cache_size=0 keeps us compatible with pooled (PgBouncer) endpoints
        # such as Neon's "-pooler" host and Supabase's transaction pooler.
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=5, statement_cache_size=0
        )
        log.info("Connected to database")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def init_schema(self) -> None:
        await self.pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ config

    async def get_config(self, guild_id: int) -> GuildConfig:
        row = await self.pool.fetchrow("SELECT * FROM guild_config WHERE guild_id = $1", guild_id)
        if row is None:
            return GuildConfig(guild_id=guild_id)
        return GuildConfig(guild_id=guild_id, **{f: row[f] for f in CONFIG_FIELDS})

    async def save_config(self, cfg: GuildConfig) -> None:
        values = [getattr(cfg, f) for f in CONFIG_FIELDS]
        cols = ", ".join(CONFIG_FIELDS)
        params = ", ".join(f"${i}" for i in range(2, len(CONFIG_FIELDS) + 2))
        updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in CONFIG_FIELDS)
        await self.pool.execute(
            f"INSERT INTO guild_config (guild_id, {cols}) VALUES ($1, {params}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates}, updated_at = now()",
            cfg.guild_id,
            *values,
        )

    # ------------------------------------------------------------------- teams

    async def upsert_team(
        self, tournament: str, participant_id: int, name: str, group_player_ids: Iterable[int]
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO teams (tournament, participant_id, name, group_player_ids)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tournament, participant_id)
            DO UPDATE SET name = EXCLUDED.name, group_player_ids = EXCLUDED.group_player_ids
            """,
            tournament,
            participant_id,
            name,
            list(group_player_ids),
        )

    async def set_team_role(self, tournament: str, participant_id: int, role_id: Optional[int]) -> bool:
        result = await self.pool.execute(
            "UPDATE teams SET role_id = $3 WHERE tournament = $1 AND participant_id = $2",
            tournament,
            participant_id,
            role_id,
        )
        return result.endswith(" 1")

    async def get_teams(self, tournament: str) -> List[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM teams WHERE tournament = $1 ORDER BY lower(name)", tournament
        )

    # ----------------------------------------------------------------- matches

    async def get_match(self, match_id: int) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(MATCH_SELECT + " WHERE m.match_id = $1", match_id)

    async def get_match_by_thread(self, thread_id: int) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(MATCH_SELECT + " WHERE m.thread_id = $1", thread_id)

    async def list_matches(self, tournament: str, statuses: Optional[List[str]] = None) -> List[asyncpg.Record]:
        if statuses:
            return await self.pool.fetch(
                MATCH_SELECT + " WHERE m.tournament = $1 AND m.status = ANY($2::text[]) ORDER BY m.created_at",
                tournament,
                statuses,
            )
        return await self.pool.fetch(
            MATCH_SELECT + " WHERE m.tournament = $1 ORDER BY m.created_at", tournament
        )

    async def insert_match(
        self,
        *,
        match_id: int,
        tournament: str,
        guild_id: int,
        round_no: int,
        group_id: Optional[int],
        player1_id: int,
        player2_id: int,
        team1_participant_id: int,
        team2_participant_id: int,
        allow_ties: bool,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO matches (match_id, tournament, guild_id, round, group_id, player1_id, player2_id,
                                 team1_participant_id, team2_participant_id, allow_ties)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (match_id) DO NOTHING
            """,
            match_id,
            tournament,
            guild_id,
            round_no,
            group_id,
            player1_id,
            player2_id,
            team1_participant_id,
            team2_participant_id,
            allow_ties,
        )

    async def delete_match(self, match_id: int) -> None:
        await self.pool.execute("DELETE FROM matches WHERE match_id = $1", match_id)

    async def set_match_thread(self, match_id: int, thread_id: int) -> None:
        await self.pool.execute("UPDATE matches SET thread_id = $2 WHERE match_id = $1", match_id, thread_id)

    async def submit_score(self, match_id: int, side: int, user_id: int, score1: int, score2: int) -> SubmitResult:
        """Record one captain's blind report and compare it with the other side's, atomically."""
        if side not in (1, 2):
            raise ValueError("side must be 1 or 2")
        mine, other = f"sub{side}", f"sub{3 - side}"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1 FOR UPDATE", match_id)
                if row is None or row["status"] != OPEN:
                    return SubmitResult("closed", None)

                already = row[f"{mine}_score1"] is not None
                await conn.execute(
                    f"UPDATE matches SET {mine}_score1 = $2, {mine}_score2 = $3, {mine}_user_id = $4, "
                    f"{mine}_at = now() WHERE match_id = $1",
                    match_id,
                    score1,
                    score2,
                    user_id,
                )

                o1, o2 = row[f"{other}_score1"], row[f"{other}_score2"]
                if o1 is None:
                    outcome = "updated" if already else "recorded"
                elif (o1, o2) == (score1, score2):
                    await conn.execute(
                        "UPDATE matches SET status = $2, final_score1 = $3, final_score2 = $4, "
                        "result_source = 'captains', verified_at = now() WHERE match_id = $1",
                        match_id,
                        VERIFIED,
                        score1,
                        score2,
                    )
                    outcome = "verified"
                else:
                    await conn.execute(
                        "UPDATE matches SET sub1_score1 = NULL, sub1_score2 = NULL, sub1_user_id = NULL, sub1_at = NULL, "
                        "sub2_score1 = NULL, sub2_score2 = NULL, sub2_user_id = NULL, sub2_at = NULL, "
                        "mismatch_count = mismatch_count + 1 WHERE match_id = $1",
                        match_id,
                    )
                    outcome = "mismatch"

                match = await conn.fetchrow(MATCH_SELECT + " WHERE m.match_id = $1", match_id)
                other_report = (o1, o2) if o1 is not None else None
                return SubmitResult(outcome, match, other_report)

    async def force_result(self, match_id: int, score1: int, score2: int, admin_id: int) -> bool:
        result = await self.pool.execute(
            """
            UPDATE matches SET status = $2, final_score1 = $3, final_score2 = $4,
                   result_source = 'admin', forced_by = $5, verified_at = now()
            WHERE match_id = $1 AND status IN ('open', 'verified')
            """,
            match_id,
            VERIFIED,
            score1,
            score2,
            admin_id,
        )
        return result.endswith(" 1")

    async def reset_submissions(self, match_id: int) -> bool:
        result = await self.pool.execute(
            "UPDATE matches SET sub1_score1 = NULL, sub1_score2 = NULL, sub1_user_id = NULL, sub1_at = NULL, "
            "sub2_score1 = NULL, sub2_score2 = NULL, sub2_user_id = NULL, sub2_at = NULL "
            "WHERE match_id = $1 AND status = 'open'",
            match_id,
        )
        return result.endswith(" 1")

    async def mark_completed(
        self, match_id: int, *, source: Optional[str] = None, challonge_scores: Optional[str] = None
    ) -> None:
        await self.pool.execute(
            "UPDATE matches SET status = $2, completed_at = now(), "
            "result_source = COALESCE($3, result_source), challonge_scores = COALESCE($4, challonge_scores) "
            "WHERE match_id = $1",
            match_id,
            COMPLETED,
            source,
            challonge_scores,
        )

    # ------------------------------------------------------------ stage posts

    async def claim_stage_post(self, tournament: str, stage_key: str) -> bool:
        """Returns True the first time a stage key is claimed, False afterwards."""
        row = await self.pool.fetchrow(
            "INSERT INTO stage_posts (tournament, stage_key) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING RETURNING stage_key",
            tournament,
            stage_key,
        )
        return row is not None
