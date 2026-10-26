import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.constants import DEFAULT_MINIMUM_RATING_FOR_THEME_BASED_MOVIE, DEFAULT_MINIMUM_RATING_FOR_THEME_BASED_TV
from app.services.recommendation.filtering import (
    RecommendationFiltering,
    apply_discover_filters,
    filter_by_genres,
    filter_items_by_settings,
    filter_watched_by_imdb,
)
from app.services.recommendation.item_based import ItemBasedService
from app.services.recommendation.metadata import RecommendationMetadata
from app.services.recommendation.scoring import RecommendationScoring
from app.services.recommendation.utils import content_type_to_mtype, resolve_tmdb_id

BASE_FLOOR = 6.5
# A row built around a title the user rated this high shouldn't be padded with ordinary ones.
STRONG_SEED_RATING = 8.0
STRONG_SEED_FLOOR = 7.0
MIN_VOTES = 100
# Rows are asked for this much more than they show: what enrichment drops (already
# watched under another id, no IMDb id) would otherwise leave them short.
HEADROOM = 1.25
KEYWORDS_USED = 8

# Audience and format genres. A seed without one of these doesn't want it in its row:
# an animated or documentary title isn't a match for a live-action drama.
OFF_TOPIC_GENRES = frozenset({16, 99, 10751, 10762, 10763, 10764, 10767})


@dataclass(frozen=True)
class Seed:
    genres: tuple[int, ...]
    language: str | None
    year: int | None
    keywords: tuple[int, ...]

    @classmethod
    def from_details(cls, details: dict[str, Any]) -> "Seed":
        block = details.get("keywords") or {}
        keywords = tuple(k["id"] for k in (block.get("keywords") or block.get("results") or []))
        return cls(
            genres=tuple(g["id"] for g in details.get("genres", [])),
            language=details.get("original_language"),
            year=RecommendationMetadata.extract_year(details),
            keywords=keywords[:KEYWORDS_USED],
        )


def row_size(limit: int) -> int:
    return math.ceil(limit * HEADROOM)


def rating_floor(seed_rating: float | None) -> float:
    return STRONG_SEED_FLOOR if seed_rating is not None and seed_rating >= STRONG_SEED_RATING else BASE_FLOOR


def clears_floor(item: dict[str, Any], floor: float, min_votes: int = MIN_VOTES) -> bool:
    """A weighted rating shrinks a thin sample toward the floor itself, so a title with
    a handful of votes can't pass on a lucky average, and one below the floor never does."""
    votes = item.get("vote_count") or 0
    if votes < min_votes:
        return False
    return RecommendationScoring.weighted_rating(item.get("vote_average"), votes, C=floor) >= floor


def quality(item: dict[str, Any], mtype: str) -> float:
    prior = (
        DEFAULT_MINIMUM_RATING_FOR_THEME_BASED_TV
        if mtype in ("tv", "series")
        else DEFAULT_MINIMUM_RATING_FOR_THEME_BASED_MOVIE
    )
    wr = RecommendationScoring.weighted_rating(item.get("vote_average"), item.get("vote_count"), C=prior)
    return RecommendationScoring.normalize(wr, 6.0, 8.5)


def has_off_topic(item_genres: Iterable[int], seed_genres: Iterable[int]) -> bool:
    return bool(set(item_genres) & (OFF_TOPIC_GENRES - set(seed_genres)))


def on_topic(item_genres: Iterable[int], seed_genres: Iterable[int]) -> bool:
    """Shares a genre with the seed and brings no audience or format genre it lacks. A
    candidate with no genres (Simkl's arrive bare) can't be judged yet and passes."""
    genres = set(item_genres)
    if not genres:
        return True
    return bool(genres & set(seed_genres)) and not has_off_topic(genres, seed_genres)


def relevance(item: dict[str, Any], seed: Seed) -> float:
    genres = set(item.get("genre_ids") or ())
    if genres:
        overlap = len(genres & set(seed.genres)) / len(genres | set(seed.genres))
        lead = float(bool(seed.genres) and seed.genres[0] in genres)
    else:
        overlap = lead = 0.5

    rec_rank = item.get("_rec_rank")
    source = 0.6 * (1 - min(rec_rank, 60) / 60 if rec_rank is not None else 0.0)
    source += 0.4 * float(bool(item.get("_in_similar")))
    keywords = {0: 0.0, 1: 0.6, 2: 1.0}[item.get("_kw", 0)]
    language = float(item.get("original_language") == seed.language)
    year = RecommendationMetadata.extract_year(item)
    era = max(0.0, 1 - abs(year - seed.year) / 30) if year and seed.year else 0.5
    return 0.35 * overlap + 0.10 * lead + 0.20 * source + 0.20 * keywords + 0.075 * language + 0.075 * era


def rank(items: Iterable[dict[str, Any]], seed: Seed, mtype: str, floor: float) -> list[dict[str, Any]]:
    """Relevance to the seed, scaled by quality. Quality only scales it, so a close match
    at 7.2 outranks a loose one at 8.5."""
    kept = [i for i in items if on_topic(i.get("genre_ids") or (), seed.genres) and clears_floor(i, floor)]
    return sorted(kept, key=lambda i: relevance(i, seed) * (0.5 + 0.5 * quality(i, mtype)), reverse=True)


class RankedItemService(ItemBasedService):
    """Item rows scored against the seed instead of served in TMDB's order.

    TMDB's /recommendations and /similar are the candidates, as in stock, plus discover
    on the seed's keywords, which is what tells a close match from a popular neighbour.
    Everything is scored on the compact shape, before enrichment, so only the titles that
    make the row cost the two detail calls each.
    """

    async def get_recommendations_for_item(
        self,
        item_id: str,
        content_type: str,
        watched_tmdb: set[int] | None = None,
        watched_imdb: set[str] | None = None,
        limit: int = 20,
        seed_rating: float | None = None,
    ) -> list[dict[str, Any]]:
        tmdb_id = await resolve_tmdb_id(item_id, self.tmdb_service)
        if not tmdb_id:
            return []
        mtype = content_type_to_mtype(content_type)
        fetch_details = self.tmdb_service.get_movie_details if mtype == "movie" else self.tmdb_service.get_tv_details
        seed = Seed.from_details(await fetch_details(tmdb_id))
        floor = rating_floor(seed_rating)
        excluded = RecommendationFiltering.get_excluded_genre_ids(self.user_settings, content_type)
        skip = (watched_tmdb or set()) | {tmdb_id}

        pool: dict[int, dict[str, Any]] = {}
        await self._collect(pool, item_id, tmdb_id, mtype, seed)
        ranked = self._ranked(pool, seed, mtype, floor, skip, excluded)

        for params in self._widenings(seed):
            if len(ranked) >= limit:
                break
            await self._collect_discover(pool, mtype, params, pages=range(1, 4))
            ranked = self._ranked(pool, seed, mtype, floor, skip, excluded)

        enriched = await RecommendationMetadata.fetch_batch(
            self.tmdb_service, ranked[: row_size(limit)], content_type, user_settings=self.user_settings
        )
        return filter_watched_by_imdb(enriched, watched_imdb or set())

    def _ranked(
        self,
        pool: dict[int, dict[str, Any]],
        seed: Seed,
        mtype: str,
        floor: float,
        skip: set[int],
        excluded: list[int],
    ) -> list[dict[str, Any]]:
        # Simkl candidates arrive with the band already applied by Simkl itself.
        tmdb = [i for i in pool.values() if "_simkl_id" not in i]
        simkl = [i for i in pool.values() if "_simkl_id" in i]
        candidates = filter_items_by_settings(tmdb, self.user_settings) + simkl
        return rank(filter_by_genres(candidates, skip, excluded), seed, mtype, floor)

    async def _collect(self, pool: dict[int, dict[str, Any]], item_id: str, tmdb_id: int, mtype: str, seed: Seed):
        tmdb = self.tmdb_service
        jobs: dict[str, Any] = {
            **{f"rec{p}": tmdb.get_recommendations(tmdb_id, mtype, page=p) for p in (1, 2, 3)},
            **{f"sim{p}": tmdb.get_similar(tmdb_id, mtype, page=p) for p in (1, 2)},
        }
        if seed.keywords:
            # Both queries use the seed's own keywords: the pair together is a close
            # match, any of the eight a looser one.
            jobs["kw2"] = self._discover(mtype, {"with_keywords": ",".join(map(str, seed.keywords[:2]))}, 1)
            jobs["kw8"] = self._discover(mtype, {"with_keywords": "|".join(map(str, seed.keywords))}, 1)
        simkl_key = self.user_settings.simkl_api_key if self.user_settings else None
        if simkl_key:
            jobs["simkl"] = self._fetch_candidates_from_simkl(item_id, mtype)

        results = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
        for name, result in results.items():
            if isinstance(result, BaseException):
                logger.warning(f"Candidates '{name}' for {item_id} failed: {type(result).__name__}")
                continue
            if name == "simkl":
                for item in result:
                    if item.get("id"):
                        pool.setdefault(item["id"], dict(item))
                continue
            for position, item in enumerate(result.get("results", [])):
                if not item.get("id"):
                    continue
                # A copy: the page is an alru_cache entry shared with other rows.
                entry = pool.setdefault(item["id"], dict(item))
                if name.startswith("rec"):
                    place = (int(name[3:]) - 1) * 20 + position
                    entry["_rec_rank"] = min(entry.get("_rec_rank", place), place)
                elif name.startswith("sim"):
                    entry["_in_similar"] = True
                else:
                    entry["_kw"] = max(entry.get("_kw", 0), 2 if name == "kw2" else 1)

    def _discover(self, mtype: str, params: dict[str, Any], page: int):
        sort_by = RecommendationFiltering.get_sort_by_preference(self.user_settings)
        merged = apply_discover_filters({"sort_by": sort_by, **params}, self.user_settings)
        return self.tmdb_service.get_discover(mtype, page=page, **merged)

    async def _collect_discover(self, pool: dict[int, dict[str, Any]], mtype: str, params: dict[str, Any], pages):
        results = await asyncio.gather(*(self._discover(mtype, params, p) for p in pages), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning(f"Widening discover failed: {type(result).__name__}")
                continue
            for item in result.get("results", []):
                if item.get("id"):
                    pool.setdefault(item["id"], dict(item))

    @staticmethod
    def _widenings(seed: Seed) -> list[dict[str, Any]]:
        """Run in turn, only while the row is short: the seed's two leading genres
        together, then its leading genre alone, best rated first."""
        best = {"sort_by": "vote_average.desc", "vote_count.gte": 500}
        steps = []
        if len(seed.genres) >= 2:
            steps.append({**best, "with_genres": ",".join(map(str, seed.genres[:2]))})
        if seed.genres:
            steps.append({**best, "with_genres": str(seed.genres[0])})
        return steps
