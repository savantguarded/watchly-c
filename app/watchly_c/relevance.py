import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.constants import (
    DEFAULT_CONCURRENCY_LIMIT,
    DEFAULT_MINIMUM_RATING_FOR_THEME_BASED_MOVIE,
    DEFAULT_MINIMUM_RATING_FOR_THEME_BASED_TV,
)
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
from app.services.tmdb.service import TMDBService
from app.watchly_c.fit import keyword_ids
from app.watchly_c.meta_keywords import is_telling_keyword

BASE_FLOOR = 6.5
# A row built around a title the user rated this high shouldn't be padded with ordinary ones.
STRONG_SEED_RATING = 8.0
STRONG_SEED_FLOOR = 7.0
MIN_VOTES = 100
# Rows are asked for this much more than they show: what enrichment drops (already
# watched under another id, no IMDb id) would otherwise leave them short.
HEADROOM = 1.25
KEYWORDS_USED = 8
# Candidates judged on their details per row, and how many of the seed's closest
# neighbours lend their own recommendations.
SHORTLIST = 50
NEIGHBOURS = 3
WIDENING_KEYWORDS = 3

# Audience and format genres. A seed without one of these doesn't want it in its row:
# an animated or documentary title isn't a match for a live-action drama.
OFF_TOPIC_GENRES = frozenset({16, 99, 10751, 10762, 10763, 10764, 10767})


# Crew whose other work is the same kind of film: who made it and who wrote it.
CREW_JOBS = frozenset({"Director", "Screenplay", "Writer", "Story", "Novel", "Characters", "Creator"})
CAST_DEPTH = 8


def people(details: dict[str, Any]) -> tuple[frozenset[int], frozenset[int]]:
    """(crew, leading cast) ids of a title, from its details."""
    credits = details.get("credits") or {}
    crew = {c["id"] for c in credits.get("crew") or () if c.get("job") in CREW_JOBS}
    crew |= {c["id"] for c in details.get("created_by") or ()}
    cast = {c["id"] for c in sorted(credits.get("cast") or (), key=lambda c: c.get("order", 99))[:CAST_DEPTH]}
    return frozenset(crew), frozenset(cast)


def collection_of(details: dict[str, Any]) -> int | None:
    return (details.get("belongs_to_collection") or {}).get("id")


@dataclass(frozen=True)
class Seed:
    genres: tuple[int, ...]
    language: str | None
    year: int | None
    keywords: tuple[int, ...]
    every_keyword: frozenset[int] = frozenset()
    # Keywords specific enough that sharing one ties a title to the seed.
    telling: tuple[int, ...] = ()
    crew: frozenset[int] = frozenset()
    cast: frozenset[int] = frozenset()
    collection: int | None = None

    @classmethod
    def from_details(cls, details: dict[str, Any]) -> "Seed":
        block = details.get("keywords") or {}
        tags = [(k["id"], k.get("name")) for k in (block.get("keywords") or block.get("results") or [])]
        telling = tuple(kid for kid, name in tags if is_telling_keyword(name))
        ordered = telling + tuple(kid for kid, _ in tags if kid not in telling)
        crew, cast = people(details)
        return cls(
            genres=tuple(g["id"] for g in details.get("genres", [])),
            language=details.get("original_language"),
            year=RecommendationMetadata.extract_year(details),
            keywords=ordered[:KEYWORDS_USED],
            every_keyword=frozenset(kid for kid, _ in tags),
            telling=telling,
            crew=crew,
            cast=cast,
            collection=collection_of(details),
        )


def tmdb_signal(item: dict[str, Any]) -> float:
    """How strongly TMDB itself ties a candidate to the seed: its place in the seed's
    recommendations, a place in its similar titles, or a place in a neighbour's."""
    rec_rank = item.get("_rec_rank")
    signal = 0.6 * (1 - min(rec_rank, 60) / 60) if rec_rank is not None else 0.0
    signal += 0.4 * float(bool(item.get("_in_similar")))
    return max(signal, 0.3 * float(bool(item.get("_hop"))))


def content_match(
    details: dict[str, Any], seed: Seed, signal: float = 0.0, genres: Iterable[int] | None = None
) -> tuple[float, bool]:
    """(how close a title is to the seed, whether anything ties it to the seed at all).

    A title is tied by a telling keyword, the same director, writer or creator, two of the
    leads, the same franchise, or by TMDB recommending it alongside a shared genre majority
    and keyword. Sharing a genre and a stock tag ("murder", "based on novel") isn't a tie:
    that is how rows filled with titles that only looked related in TMDB's genre list.
    `genres` replaces the seed's for a title of the other type."""
    seed_genres = set(seed.genres if genres is None else genres)
    item_genres = {g["id"] for g in details.get("genres") or ()} or set(details.get("genre_ids") or ())
    overlap = len(item_genres & seed_genres) / len(item_genres | seed_genres) if item_genres | seed_genres else 0.0
    keywords = keyword_ids(details)
    telling = len(keywords & set(seed.telling))
    loose = len(keywords & seed.every_keyword) - telling
    crew, cast = people(details)
    shared_crew, shared_cast = len(crew & seed.crew), len(cast & seed.cast)
    franchise = seed.collection is not None and collection_of(details) == seed.collection
    tied = (
        telling >= 1
        or shared_crew >= 1
        or shared_cast >= 2
        or franchise
        # A seed with no telling keyword (series often have few) leans on TMDB's own tie.
        or (signal >= 0.7 and overlap >= 0.5 and (loose >= 1 or not seed.telling))
    )

    year = RecommendationMetadata.extract_year(details)
    era = max(0.0, 1 - abs(year - seed.year) / 30) if year and seed.year else 0.5
    score = (
        0.30 * (0.85 * min(1.0, telling / 3) + 0.15 * min(1.0, loose / 4))
        + 0.20 * overlap
        + 0.18 * signal
        + 0.14 * min(1.0, (shared_crew + 0.5 * shared_cast) / 1.5)
        + 0.10 * franchise
        + 0.04 * era
        + 0.04 * float(details.get("original_language") == seed.language)
    )
    return score, tied


def by_match(
    candidates: list[dict[str, Any]],
    details: dict[int, dict[str, Any]],
    seed: Seed,
    mtype: str,
    genres: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    """Candidates tied to the seed (all of them with the fit gate off), closest first,
    scaled by quality so a close 7.2 still beats a loose 8.5."""
    scored = []
    for item in candidates:
        if item["id"] not in details:
            continue
        score, tied = content_match(details[item["id"]], seed, tmdb_signal(item), genres)
        if tied or not settings.FIT_GATE:
            scored.append((score * (0.65 + 0.35 * quality(item, mtype)), item))
    return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)]


async def details_for(tmdb_service: TMDBService, items: list[dict[str, Any]], mtype: str) -> dict[int, dict[str, Any]]:
    """Details by TMDB id. They are cached per process, so the row's enrichment that follows
    reads them again for free. A title whose details fail is left out of the row."""
    fetch = tmdb_service.get_movie_details if mtype == "movie" else tmdb_service.get_tv_details
    sem = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)

    async def one(tmdb_id: int) -> dict[str, Any] | None:
        async with sem:
            try:
                return await fetch(tmdb_id)
            except Exception as e:
                logger.debug(f"Details for {mtype} {tmdb_id} failed: {type(e).__name__}")
                return None

    results = await asyncio.gather(*(one(i["id"]) for i in items))
    return {item["id"]: d for item, d in zip(items, results) if d}


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


def language_ok(item: dict[str, Any], seed_language: str | None) -> bool:
    """English and the seed's own language. A subtitled title only comes up in a row
    built around a title in that language. Bare Simkl candidates carry no language."""
    language = item.get("original_language")
    return not language or language == "en" or language == seed_language


def relevance(item: dict[str, Any], seed: Seed) -> float:
    genres = set(item.get("genre_ids") or ())
    if genres:
        overlap = len(genres & set(seed.genres)) / len(genres | set(seed.genres))
        lead = float(bool(seed.genres) and seed.genres[0] in genres)
    else:
        overlap = lead = 0.5

    source = tmdb_signal(item)
    keywords = {0: 0.0, 1: 0.6, 2: 1.0}[item.get("_kw", 0)]
    language = float(item.get("original_language") == seed.language)
    year = RecommendationMetadata.extract_year(item)
    era = max(0.0, 1 - abs(year - seed.year) / 30) if year and seed.year else 0.5
    return 0.35 * overlap + 0.10 * lead + 0.20 * source + 0.20 * keywords + 0.075 * language + 0.075 * era


def rank(items: Iterable[dict[str, Any]], seed: Seed, mtype: str, floor: float) -> list[dict[str, Any]]:
    """Relevance to the seed, scaled by quality. Quality only scales it, so a close match
    at 7.2 outranks a loose one at 8.5."""
    kept = [
        i
        for i in items
        if on_topic(i.get("genre_ids") or (), seed.genres) and language_ok(i, seed.language) and clears_floor(i, floor)
    ]
    return sorted(kept, key=lambda i: relevance(i, seed) * (0.5 + 0.5 * quality(i, mtype)), reverse=True)


class RankedItemService(ItemBasedService):
    """Item rows built from what is actually like the seed.

    Candidates are TMDB's recommendations and similar titles, the recommendations of the
    seed's closest neighbours, and discover on the seed's telling keywords. They are
    shortlisted on the compact shape, then judged on their details (keywords, people,
    franchise) and kept only when something ties them to the seed. A row that runs out
    stays short: padding it with the best-rated titles of the seed's genres is what put
    unrelated films in it.
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
        await self._collect_neighbours(pool, mtype, self._ranked(pool, seed, mtype, floor, skip, excluded))
        matched = await self._matched(pool, seed, mtype, floor, skip, excluded)
        if len(matched) < row_size(limit):
            for params in self._widenings(seed):
                await self._collect_discover(pool, mtype, params, pages=range(1, 3))
            matched = await self._matched(pool, seed, mtype, floor, skip, excluded)

        enriched = await RecommendationMetadata.fetch_batch(
            self.tmdb_service, matched[: row_size(limit)], content_type, user_settings=self.user_settings
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

    async def _matched(
        self,
        pool: dict[int, dict[str, Any]],
        seed: Seed,
        mtype: str,
        floor: float,
        skip: set[int],
        excluded: list[int],
    ) -> list[dict[str, Any]]:
        shortlist = self._ranked(pool, seed, mtype, floor, skip, excluded)[:SHORTLIST]
        return by_match(shortlist, await details_for(self.tmdb_service, shortlist, mtype), seed, mtype)

    async def _collect(self, pool: dict[int, dict[str, Any]], item_id: str, tmdb_id: int, mtype: str, seed: Seed):
        tmdb = self.tmdb_service
        jobs: dict[str, Any] = {
            **{f"rec{p}": tmdb.get_recommendations(tmdb_id, mtype, page=p) for p in (1, 2, 3)},
            **{f"sim{p}": tmdb.get_similar(tmdb_id, mtype, page=p) for p in (1, 2)},
        }
        if seed.keywords:
            # The seed's own keywords, telling ones first: the pair together is a close
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

    async def _collect_neighbours(self, pool: dict[int, dict[str, Any]], mtype: str, ranked: list[dict[str, Any]]):
        """Recommendations of the titles TMDB ties closest to the seed (in both its
        recommended and similar lists). They stay near the seed where a genre query drifts."""
        neighbours = [i["id"] for i in ranked if i.get("_in_similar") and i.get("_rec_rank", 99) < 20][:NEIGHBOURS]
        results = await asyncio.gather(
            *(self.tmdb_service.get_recommendations(n, mtype, page=1) for n in neighbours), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning(f"Neighbour recommendations failed: {type(result).__name__}")
                continue
            for item in result.get("results", []):
                if item.get("id"):
                    pool.setdefault(item["id"], {**item, "_hop": True})

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
                    entry = pool.setdefault(item["id"], dict(item))
                    entry["_kw"] = max(entry.get("_kw", 0), 1)

    @staticmethod
    def _widenings(seed: Seed) -> list[dict[str, Any]]:
        """Each of the seed's leading telling keywords within its leading genre, best known
        first. Only run while the row is short."""
        if not seed.genres:
            return []
        return [
            {"with_keywords": str(k), "with_genres": str(seed.genres[0]), "sort_by": "vote_count.desc"}
            for k in seed.telling[:WIDENING_KEYWORDS]
        ]
