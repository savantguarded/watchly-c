import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.settings import UserSettings
from app.models.library import LibraryCollection
from app.models.profile import TasteProfile
from app.services.user_cache import user_cache
from app.watchly_c.mixed_rows import OTHER_TYPE, interleave
from app.watchly_c.watched_tmdb import watched_tmdb_ids

# What a row needs about one type: its taste profile (None when that type has none yet)
# and the titles already watched, by TMDB and IMDb id.
TypeState = tuple[TasteProfile | None, set[int], set[str]]
# A row asks each type for a little more than half of what it shows, so the row still
# fills when one side comes up short.
SHARE = 0.6


def enabled_types(user_settings: UserSettings, config_id: str) -> list[str]:
    config = next((c for c in user_settings.catalogs if c.id == config_id), None)
    if not config:
        return []
    return [t for t, on in (("movie", config.enabled_movie), ("series", config.enabled_series)) if on]


async def other_type_state(
    token: str, library: LibraryCollection, content_type: str, user_settings: UserSettings
) -> TypeState:
    """The other type's state for a mixed row. Served from the cached profile; a row
    never builds one, since that is a full library pass inside a request."""
    other = OTHER_TYPE[content_type]
    cached = await user_cache.get_profile_and_watched_sets(token, other)
    if cached:
        return cached
    watched = await watched_tmdb_ids(library, other, user_settings) if settings.WATCHED_TMDB_IDS else set()
    return None, watched, library.all_imdb_ids()


async def row_states(
    token: str,
    library: LibraryCollection,
    config_id: str,
    content_type: str,
    own: TypeState,
    user_settings: UserSettings,
) -> tuple[list[str], dict[str, TypeState]]:
    """The types a mixed row draws on, the requested type first, and each one's state."""
    types = enabled_types(user_settings, config_id)
    states = {content_type: own}
    if OTHER_TYPE[content_type] in types:
        states[OTHER_TYPE[content_type]] = await other_type_state(token, library, content_type, user_settings)
    return [content_type] + [t for t in types if t != content_type], states


async def blend(
    types: list[str],
    states: dict[str, TypeState],
    claimed: dict[str, set[int]],
    limit: int,
    build: Callable[[str, TypeState, int], Awaitable[list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """`build` runs for each type, each side leaving out what other rows already show,
    and the results alternate card by card. One side failing leaves the row to the other."""
    share = limit if len(types) == 1 else math.ceil(limit * SHARE)
    jobs = []
    for content_type in types:
        profile, watched_tmdb, watched_imdb = states[content_type]
        state = (profile, watched_tmdb | claimed.get(content_type, set()), watched_imdb)
        jobs.append(build(content_type, state, share))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    lists = []
    for content_type, result in zip(types, results):
        if isinstance(result, BaseException):
            if len(types) == 1:
                raise result
            logger.warning(f"{content_type} half of a mixed row failed: {type(result).__name__}")
            result = []
        lists.append(result)
    if len(lists) == 1:
        return lists[0][:limit]
    return interleave(lists[0], lists[1], limit, every=2)
