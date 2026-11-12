"""Render Challonge's live bracket SVG to a PNG that Discord can display."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .challonge import USER_AGENT

log = logging.getLogger(__name__)

MAX_DISCORD_BYTES = 9_500_000


def bracket_svg_url(tournament: dict) -> Optional[str]:
    url = tournament.get("live_image_url")
    if not url and tournament.get("full_challonge_url"):
        url = f"{tournament['full_challonge_url']}.svg"
    if url and url.startswith("//"):
        url = "https:" + url
    return url


async def _fetch_svg(url: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                return await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.exception("Couldn't download bracket SVG from %s", url)
        return None


async def render_bracket_png(svg_url: str) -> Optional[bytes]:
    """Returns PNG bytes, or None if rendering isn't possible (caller falls back to a link)."""
    svg = await _fetch_svg(svg_url)
    if not svg:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("playwright is not installed; bracket images are disabled")
        return None

    # Embed in an HTML page with a white background so it's readable in Discord's dark theme.
    html = (
        "<!doctype html><html><head><style>"
        "html,body{margin:0;padding:0;background:#ffffff}svg{display:block}"
        "</style></head><body>" + svg + "</body></html>"
    )
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                for scale in (2, 1):
                    page = await browser.new_page(
                        viewport={"width": 1280, "height": 720}, device_scale_factor=scale
                    )
                    await page.set_content(html, wait_until="load")
                    png = await page.locator("svg").first.screenshot(type="png")
                    await page.close()
                    if len(png) <= MAX_DISCORD_BYTES:
                        return png
                return None
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001 - any render failure should just fall back to a link
        log.exception("Couldn't render bracket image")
        return None
