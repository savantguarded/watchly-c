import asyncio

import redis.asyncio as redis
from loguru import logger

from app.core.constants import DEFAULT_CONCURRENCY_LIMIT, IMDB_TO_TMDB_KEY
from app.core.settings import UserSettings, resolve_tmdb_api_key
from app.models.library import LibraryCollection
from app.services.redis_service import redis_service
from app.services.tmdb.service import get_tmdb_service


async def watched_tmdb_ids(library: LibraryCollection, content_type: str, user_settings: UserSettings) -> set[int]:
    """TMDB ids of everything watched of one type, for an external-source library.

    The engines drop watched titles by TMDB id before they truncate to a row, and by
    IMDb id only after enrichment. External histories are keyed on IMDb ids, so the
    TMDB set came back empty: the cut to 2x the row length kept watched titles, and
    the IMDb filter afterwards emptied the row. A heavy watcher saw rows of 3.

    The IMDb-to-TMDB mapping never changes, so it lives in one shared Redis hash and
    each title is looked up once, ever. Misses aren't stored: the lookup can't tell
    "TMDB has no such title" from a failed request, and a stored failure would hide
    that title's matches for good.
    """
    imdb_ids = sorted({i.id.split(":")[0] for i in library.for_type(content_type).all_items_with_removed()})
    imdb_ids = [i for i in imdb_ids if i.startswith("tt")]
    if not imdb_ids:
        return set()

    try:
        client = await redis_service.get_client()
        known = dict(zip(imdb_ids, await client.hmget(IMDB_TO_TMDB_KEY, imdb_ids)))
    except (redis.RedisError, OSError) as e:
        # Without the map the rows are what stock Watchly serves: shorter, not wrong.
        logger.warning(f"IMDb-to-TMDB map unavailable ({type(e).__name__}); watched titles filtered late")
        return set()
    missing = [i for i, v in known.items() if v is None]

    if missing:
        tmdb_service = get_tmdb_service(language=user_settings.language, api_key=resolve_tmdb_api_key(user_settings))
        sem = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)

        async def find(imdb_id: str) -> str | None:
            async with sem:
                tmdb_id, mtype = await tmdb_service.find_by_imdb_id(imdb_id)
            return f"{mtype}:{tmdb_id}" if tmdb_id else None

        found = {i: v for i, v in zip(missing, await asyncio.gather(*(find(i) for i in missing))) if v}
        if found:
            try:
                await client.hset(IMDB_TO_TMDB_KEY, mapping=found)
            except (redis.RedisError, OSError) as e:
                logger.warning(
                    f"Could not store {len(found)} TMDB ids ({type(e).__name__}); they'll be looked up again"
                )
        known.update(found)
        logger.info(f"Resolved {len(found)} of {len(missing)} new watched {content_type} titles to TMDB ids")

    wanted = "movie" if content_type == "movie" else "tv"
    return {int(v.split(":")[1]) for v in known.values() if v and v.split(":")[0] == wanted}
