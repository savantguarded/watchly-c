import asyncio
import random
from collections.abc import Callable
from datetime import datetime
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.settings import CatalogConfig, UserSettings
from app.models.library import LibraryCollection, StremioLibraryItem
from app.services.recommendation.filtering import (
    RecommendationFiltering,
    apply_discover_filters,
    filter_by_genres,
    filter_items_by_settings,
    filter_watched_by_imdb,
)
from app.services.recommendation.item_based import ItemBasedService
from app.services.recommendation.metadata import RecommendationMetadata
from app.services.recommendation.utils import resolve_tmdb_id
from app.services.tmdb.genre import movie_genres, series_genres
from app.services.tmdb.service import TMDBService
from app.watchly_c.seeds import choose_seeds

# A mixed row's id carries its seed, so a client still on yesterday's manifest gets
# the row its title promises. The seed changes once a day, so the cache key does too.
MIXED_ITEM_PREFIX = "watchly.item.mix."
OTHER_TYPE = {"movie": "series", "series": "movie"}
# Every third card in a row comes from the other type.
CROSS_EVERY = 3

_SHARED_GENRES = {gid: [gid] for gid in movie_genres.keys() & series_genres.keys()}
# TMDB splits genres differently per type; these are the ones with a clear partner.
MOVIE_TO_SERIES_GENRES: dict[int, list[int]] = {
    **_SHARED_GENRES,
    28: [10759],
    12: [10759],
    14: [10765],
    878: [10765],
    10752: [10768],
}
SERIES_TO_MOVIE_GENRES: dict[int, list[int]] = {
    **_SHARED_GENRES,
    10759: [28, 12],
    10765: [878, 14],
    10768: [10752],
    10762: [10751],
}


def _item_config(user_settings: UserSettings) -> CatalogConfig | None:
    return next((c for c in user_settings.catalogs if c.id == "watchly.item"), None)


async def mixed_item_rows(
    library: LibraryCollection,
    item_config: CatalogConfig | None,
    token: str | None,
    build_entry: Callable[..., dict[str, Any]],
    last_watched: Callable[[StremioLibraryItem], datetime],
) -> list[dict[str, Any]]:
    """`item_config.rows` "Because you watched/loved" rows in all, from seeds of either
    type. Each row is declared under its seed's type, so it opens and labels like a
    stock row, and its cards mix movies and series."""
    if not item_config or not item_config.enabled:
        return []
    types = {t for t, on in (("movie", item_config.enabled_movie), ("series", item_config.enabled_series)) if on}
    pool = [i for i in library.loved + library.liked + library.watched if i.type in types]

    if settings.WEIGHTED_ITEM_SEEDS:
        seeds = await choose_seeds(token, pool, item_config.rows, "mixed", last_watched)
    else:
        loved = sorted((i for i in pool if i.is_loved), key=last_watched, reverse=True)[:3]
        watched = sorted((i for i in pool if not i.is_loved), key=last_watched, reverse=True)[:3]
        seeds = random.sample(loved + watched, k=min(item_config.rows, len(loved + watched)))

    rows = []
    for seed in seeds:
        label = "Because you loved" if seed.is_loved else "Because you watched"
        entry = build_entry(seed, label, "watchly.item", item_config.display_at_home)
        entry["id"] = f"{MIXED_ITEM_PREFIX}{seed.id}"
        rows.append(entry)
    return rows


async def mixed_recommendations(
    item_service: ItemBasedService,
    catalog_id: str,
    seed_type: str,
    watched_tmdb: set[int],
    watched_imdb: set[str],
    library: LibraryCollection,
    limit: int,
) -> list[dict[str, Any]]:
    """The seed's own-type picks from the stock item engine, with every third card
    swapped for a title of the other type that shares the seed's keywords or genres."""
    seed_id = catalog_id.removeprefix(MIXED_ITEM_PREFIX)
    user_settings: UserSettings = item_service.user_settings
    other = OTHER_TYPE[seed_type]
    config = _item_config(user_settings)
    cross_wanted = bool(config and (config.enabled_series if other == "series" else config.enabled_movie))

    same_task = item_service.get_recommendations_for_item(seed_id, seed_type, watched_tmdb, watched_imdb, limit)
    if not cross_wanted:
        return (await same_task)[:limit]

    cross_count = limit // CROSS_EVERY + 1
    same, cross = await asyncio.gather(
        same_task,
        _cross_type(item_service.tmdb_service, user_settings, seed_id, seed_type, library, watched_imdb, cross_count),
        return_exceptions=True,
    )
    if isinstance(same, BaseException):
        raise same
    if isinstance(cross, BaseException):
        logger.warning(f"Cross-type picks for {seed_id} failed: {type(cross).__name__}")
        cross = []
    logger.info(f"Mixed row for {seed_id}: {len(same)} {seed_type}, {len(cross)} {other}")
    return interleave(same, cross, limit)


def interleave(same: list[dict[str, Any]], cross: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    same_iter, cross_iter = iter(same), iter(cross)
    while len(out) < limit:
        want_cross = len(out) % CROSS_EVERY == CROSS_EVERY - 1
        first, second = (cross_iter, same_iter) if want_cross else (same_iter, cross_iter)
        item = next(first, None) or next(second, None)
        if item is None:
            break
        if item.get("id") not in seen:
            seen.add(item.get("id"))
            out.append(item)
    return out


async def _cross_type(
    tmdb_service: TMDBService,
    user_settings: UserSettings,
    seed_id: str,
    seed_type: str,
    library: LibraryCollection,
    watched_imdb: set[str],
    count: int,
) -> list[dict[str, Any]]:
    """Titles of the other type found through TMDB discover on the seed's keywords
    and its genres, mapped across. Those matching on both lead."""
    tmdb_id = await resolve_tmdb_id(seed_id, tmdb_service)
    if not tmdb_id:
        return []
    if seed_type == "movie":
        details = await tmdb_service.get_movie_details(tmdb_id)
    else:
        details = await tmdb_service.get_tv_details(tmdb_id)

    other = OTHER_TYPE[seed_type]
    genre_map = MOVIE_TO_SERIES_GENRES if seed_type == "movie" else SERIES_TO_MOVIE_GENRES
    genres = list(dict.fromkeys(g for genre in details.get("genres", []) for g in genre_map.get(genre["id"], [])))
    keyword_block = details.get("keywords") or {}
    keywords = [k["id"] for k in (keyword_block.get("keywords") or keyword_block.get("results") or [])][:6]
    if not genres and not keywords:
        return []

    excluded = RecommendationFiltering.get_excluded_genre_ids(user_settings, other)
    params = apply_discover_filters({"without_genres": "|".join(map(str, excluded))} if excluded else {}, user_settings)
    sort_by = RecommendationFiltering.get_sort_by_preference(user_settings)

    queries = []
    if keywords:
        queries.append({"with_keywords": "|".join(map(str, keywords))})
    if genres:
        # The seed's two leading genres together, which keeps a drama/crime seed
        # from matching every drama.
        queries.append({"with_genres": ",".join(map(str, genres[:2]))})
    results = await asyncio.gather(
        *(tmdb_service.get_discover(other, sort_by=sort_by, page=1, **params, **q) for q in queries),
        return_exceptions=True,
    )

    hits: dict[int, int] = {}
    found: dict[int, dict[str, Any]] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.debug(f"Cross-type discover failed for {seed_id}: {type(result).__name__}")
            continue
        for item in result.get("results", []):
            if item.get("id"):
                found.setdefault(item["id"], item)
                hits[item["id"]] = hits.get(item["id"], 0) + 1

    ranked = sorted(found.values(), key=lambda i: -hits[i["id"]])
    ranked = filter_by_genres(filter_items_by_settings(ranked, user_settings), set(), excluded)
    # Over-fetch for what the watched filter takes out; truncate before the costly enrichment.
    enriched = await RecommendationMetadata.fetch_batch(tmdb_service, ranked[: count * 2], other, user_settings)
    return filter_watched_by_imdb(enriched, library.all_imdb_ids() | watched_imdb)[:count]
