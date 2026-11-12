"""Minimal async client for the Challonge v1 REST API.

Docs: https://api.challonge.com/v1 . v1 is deprecated (rate-limited, no new
features) but still the simplest option; everything Challonge-specific lives in
this file so it can be swapped for v2.1 later without touching the bot logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlparse

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = "KerwanCupBot/1.0 (+discord.py)"

# Path segments that can follow the tournament slug in a pasted URL.
_URL_SUFFIXES = {"module", "standings", "participants", "log", "stations", "matches"}


class ChallongeError(Exception):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def parse_tournament_ref(value: str) -> str:
    """Accept a slug ("kerwan_cup"), an org slug ("myorg-kerwan_cup") or a full URL."""
    value = value.strip()
    if "challonge.com" not in value:
        return value.strip("/")
    parsed = urlparse(value if "://" in value else f"https://{value}")
    segments = [s for s in parsed.path.split("/") if s]
    while segments and segments[-1].lower() in _URL_SUFFIXES:
        segments.pop()
    slug = segments[-1] if segments else ""
    host = (parsed.hostname or "").lower()
    subdomain = host[: -len("challonge.com")].rstrip(".")
    if subdomain and subdomain != "www":
        return f"{subdomain}-{slug}"
    return slug


class ChallongeClient:
    BASE_URL = "https://api.challonge.com/v1"

    def __init__(self, username: str, api_key: str) -> None:
        self._auth = aiohttp.BasicAuth(username, api_key)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, str]] = None,
    ) -> Any:
        session = await self._get_session()
        url = f"{self.BASE_URL}/{path}"
        try:
            async with session.request(method, url, params=params, data=data) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ChallongeError(f"Couldn't reach Challonge: {exc!r}") from exc

        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None

        if status >= 400:
            if status == 401:
                detail = "invalid Challonge username or API key"
            elif status == 404:
                detail = "not found (check the tournament URL, and that your Challonge account can manage it)"
            else:
                errors = body.get("errors") if isinstance(body, dict) else None
                detail = "; ".join(str(e) for e in errors) if errors else text[:300]
            raise ChallongeError(f"Challonge API error {status}: {detail}", status=status)
        return body

    @staticmethod
    def _t(tournament: str) -> str:
        return quote(tournament, safe="")

    async def get_tournament(self, tournament: str) -> Dict[str, Any]:
        body = await self._request("GET", f"tournaments/{self._t(tournament)}.json")
        return body["tournament"]

    async def get_participants(self, tournament: str) -> List[Dict[str, Any]]:
        body = await self._request("GET", f"tournaments/{self._t(tournament)}/participants.json")
        return [item["participant"] for item in body or []]

    async def get_matches(self, tournament: str, state: str = "all") -> List[Dict[str, Any]]:
        """state: all | pending | open | complete"""
        body = await self._request(
            "GET", f"tournaments/{self._t(tournament)}/matches.json", params={"state": state}
        )
        return [item["match"] for item in body or []]

    async def report_match(
        self, tournament: str, match_id: int, scores_csv: str, winner_id: Union[int, str]
    ) -> Dict[str, Any]:
        """Set the final score. scores_csv is player1-first, e.g. "2-1". winner_id may be "tie"."""
        body = await self._request(
            "PUT",
            f"tournaments/{self._t(tournament)}/matches/{match_id}.json",
            data={"match[scores_csv]": scores_csv, "match[winner_id]": str(winner_id)},
        )
        return body["match"]
