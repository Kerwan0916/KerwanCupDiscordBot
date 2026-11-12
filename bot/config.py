from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

REQUIRED = ("DISCORD_TOKEN", "DATABASE_URL", "CHALLONGE_USERNAME", "CHALLONGE_API_KEY", "GUILD_ID")


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_url: str
    challonge_username: str
    challonge_api_key: str
    guild_id: int


def load_settings() -> Settings:
    load_dotenv()
    missing = [key for key in REQUIRED if not os.getenv(key)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
    return Settings(
        discord_token=os.environ["DISCORD_TOKEN"],
        database_url=os.environ["DATABASE_URL"],
        challonge_username=os.environ["CHALLONGE_USERNAME"],
        challonge_api_key=os.environ["CHALLONGE_API_KEY"],
        guild_id=int(os.environ["GUILD_ID"]),
    )
