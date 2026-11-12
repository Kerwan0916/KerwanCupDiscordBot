-- Kerwan Cup bot schema. Safe to run repeatedly (applied on every startup).

-- Per-server settings, written by /setup.
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            BIGINT PRIMARY KEY,
    tournament          TEXT,     -- Challonge tournament identifier (url slug, or "subdomain-slug")
    captain_role_id     BIGINT,
    admin_role_id       BIGINT,
    matches_channel_id  BIGINT,   -- private match threads are created under this channel
    results_channel_id  BIGINT,   -- verified results are announced here
    bracket_channel_id  BIGINT,   -- bracket images (falls back to results channel)
    alerts_channel_id   BIGINT,   -- admin-only alerts (optional)
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Challonge participants and the Discord role each one is linked to.
CREATE TABLE IF NOT EXISTS teams (
    tournament        TEXT     NOT NULL,
    participant_id    BIGINT   NOT NULL,
    name              TEXT     NOT NULL,
    role_id           BIGINT,
    -- In two-stage tournaments Challonge uses separate ids for group-stage matches.
    group_player_ids  BIGINT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (tournament, participant_id)
);

-- One row per Challonge match the bot has opened a thread for.
-- "team1" is always Challonge's player1 and "team2" is player2.
CREATE TABLE IF NOT EXISTS matches (
    match_id               BIGINT PRIMARY KEY,  -- Challonge match id
    tournament             TEXT    NOT NULL,
    guild_id               BIGINT  NOT NULL,
    round                  INT     NOT NULL,     -- negative = losers bracket
    group_id               BIGINT,               -- set for group-stage matches
    player1_id             BIGINT  NOT NULL,     -- ids exactly as they appear on the Challonge match
    player2_id             BIGINT  NOT NULL,
    team1_participant_id   BIGINT  NOT NULL,
    team2_participant_id   BIGINT  NOT NULL,
    allow_ties             BOOLEAN NOT NULL DEFAULT FALSE,
    thread_id              BIGINT,
    status                 TEXT    NOT NULL DEFAULT 'open',  -- open | verified | completed

    -- Blind submissions. Both are stored as (team1 score, team2 score).
    sub1_score1 INT, sub1_score2 INT, sub1_user_id BIGINT, sub1_at TIMESTAMPTZ,
    sub2_score1 INT, sub2_score2 INT, sub2_user_id BIGINT, sub2_at TIMESTAMPTZ,
    mismatch_count         INT     NOT NULL DEFAULT 0,

    final_score1           INT,
    final_score2           INT,
    result_source          TEXT,    -- captains | admin | challonge
    forced_by              BIGINT,
    challonge_scores       TEXT,    -- scores_csv when the result was entered on Challonge directly
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at            TIMESTAMPTZ,
    completed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS matches_tournament_idx ON matches (tournament);
CREATE INDEX IF NOT EXISTS matches_thread_idx ON matches (thread_id);

-- Remembers which "round complete" bracket images were already posted.
CREATE TABLE IF NOT EXISTS stage_posts (
    tournament  TEXT NOT NULL,
    stage_key   TEXT NOT NULL,
    posted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tournament, stage_key)
);
