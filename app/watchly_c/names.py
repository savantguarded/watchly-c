import asyncio
import re

from loguru import logger

from app.core.constants import DEFAULT_CONCURRENCY_LIMIT
from app.models.history import WatchHistoryItem
from app.services.recommendation.utils import resolve_tmdb_id
from app.services.tmdb.service import TMDBService


EPISODE_SUFFIX = re.compile(r"\s*[-–:|]?\s*S\d{1,2}\s*E\d{1,3}\b.*$", re.IGNORECASE)


def series_name(name: str) -> str:
    """Nuvio can name a series row after the episode played ("Show S01E11"); a seed
    label needs the show. A name that was only a marker comes back empty, which
    `fill_missing_titles` then looks up."""
    return EPISODE_SUFFIX.sub("", name).strip()


async def fill_missing_titles(items: list[WatchHistoryItem], tmdb_service: TMDBService) -> None:
    """Nuvio's progress rows carry no title, so a title finished there and never listed as
    watched arrives nameless, and a "Because you watched" row seeded from it read "this
    title". A lookup that fails leaves the name empty rather than failing the history."""
    nameless = [item for item in items if not item.name]
    sem = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)

    async def title(item: WatchHistoryItem) -> str:
        async with sem:
            tmdb_id = await resolve_tmdb_id(item.imdb_id, tmdb_service)
            if not tmdb_id:
                return ""
            fetch = tmdb_service.get_movie_details if item.type == "movie" else tmdb_service.get_tv_details
            details = await fetch(tmdb_id)
            return details.get("title") or details.get("name") or ""

    found = await asyncio.gather(*(title(item) for item in nameless), return_exceptions=True)
    failed = 0
    for item, name in zip(nameless, found):
        if isinstance(name, Exception):
            failed += 1
        else:
            item.name = name
    if failed:
        logger.warning(f"Nuvio history: {failed} of {len(nameless)} missing titles could not be looked up")
