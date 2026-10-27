import asyncio
import itertools
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.core.constants import DISCOVER_ONLY_EXTRA
from app.core.config import settings
from app.models.profile import TasteProfile
from app.services.recommendation.filtering import (
    RecommendationFiltering,
    filter_by_genres,
    filter_items_by_settings,
    filter_watched_by_imdb,
)
from app.services.recommendation.metadata import RecommendationMetadata
from app.services.row_generator import RowDefinition
from app.services.recommendation.theme_based import ThemeBasedService
from app.services.recommendation.utils import content_type_to_mtype
from app.watchly_c.meta_keywords import is_meta_keyword
from app.watchly_c.mixed_context import TypeState, blend
from app.watchly_c.mixed_rows import MOVIE_TO_SERIES_GENRES, OTHER_TYPE, SERIES_TO_MOVIE_GENRES
from app.watchly_c.relevance import BASE_FLOOR, clears_floor, quality
from app.watchly_c.row_slots import fill_slots

MANDATORY_AXES = ("country", "era")


class WideThemeService(ThemeBasedService):
    """Genre and keyword rows held to the rating floor and topped up to a full row.

    The stock row is one discover query, plus its parts when that comes up short, and
    stops there. A narrow combination (a genre, a keyword and a country) can run out
    well before 20, and a lenient popularity setting lets poorly rated titles in. This
    drops what is below the floor, then widens only as far as the row needs.
    """

    async def get_recommendations_for_theme(
        self,
        theme_id: str,
        content_type: str,
        profile: TasteProfile | None = None,
        watched_tmdb: set[int] | None = None,
        watched_imdb: set[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        picks = await super().get_recommendations_for_theme(
            theme_id, content_type, profile, watched_tmdb, watched_imdb, limit
        )
        picks = [p for p in picks if clears_floor(p, BASE_FLOOR)]
        if len(picks) >= limit:
            return picks
        return picks + await self._top_up(picks, theme_id, content_type, profile, watched_tmdb, watched_imdb, limit)

    async def _top_up(
        self,
        picks: list[dict[str, Any]],
        theme_id: str,
        content_type: str,
        profile: TasteProfile | None,
        watched_tmdb: set[int] | None,
        watched_imdb: set[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        anchors, flavors, fallbacks = self._parse_theme_id(theme_id)
        skip = (watched_tmdb or set()) | {p["_tmdb_id"] for p in picks}
        excluded = RecommendationFiltering.get_excluded_genre_ids(self.user_settings, content_type)
        need = limit - len(picks)

        found: dict[int, dict[str, Any]] = {}
        usable: list[dict[str, Any]] = []
        favourites = await self._favourite_keywords(profile)
        for params, pages in self._ladder(anchors, flavors, fallbacks, content_type, favourites):
            for item in await self._fetch_discover_candidates(content_type, params, pages):
                found.setdefault(item["id"], item)
            unique = filter_items_by_settings(list(found.values()), self.user_settings)
            usable = [i for i in filter_by_genres(unique, skip, excluded) if clears_floor(i, BASE_FLOOR)]
            # Twice what is needed: some are dropped once enriched.
            if len(usable) >= need * 2:
                break

        mtype = content_type_to_mtype(content_type)
        usable.sort(
            key=lambda i: (self._calculate_theme_score(i, anchors, flavors, fallbacks), quality(i, mtype)),
            reverse=True,
        )
        enriched = await RecommendationMetadata.fetch_batch(
            self.tmdb_service, usable[: math.ceil(need * 1.5)], content_type, user_settings=self.user_settings
        )
        return filter_watched_by_imdb(enriched, watched_imdb or set())[:need]

    async def _favourite_keywords(self, profile: TasteProfile | None) -> list[int]:
        """The profile's four best keywords that say something about a title. Names come
        from TMDB's cached keyword lookups, which the row generator has already made."""
        if not profile:
            return []
        ids = [k for k, _ in profile.get_top_keywords(limit=8)]
        details = await asyncio.gather(*(self.tmdb_service.get_keyword_details(k) for k in ids), return_exceptions=True)
        named = [
            k for k, d in zip(ids, details) if not isinstance(d, BaseException) and not is_meta_keyword(d.get("name"))
        ]
        return named[:4]

    def _ladder(
        self, anchors: dict, flavors: dict, fallbacks: dict, content_type: str, favourites: list[int]
    ) -> list[tuple[dict[str, Any], list[int]]]:
        """Queries tried in turn, each wider than the last: the same query deeper, the
        theme's keyword joined by the profile's best ones, the anchors alone, then the
        leading genre alone. Country and era stay on throughout; they define the row."""
        axes = {**anchors, **flavors, **fallbacks}
        kept = {name: axes[name] for name in MANDATORY_AXES if name in axes}
        min_rating, min_votes = RecommendationFiltering.get_quality_thresholds(self.user_settings)
        # Best rated first, but only among titles with enough votes to mean something.
        best = {
            "sort_by": "vote_average.desc",
            "vote_count.gte": max(300, min_votes),
            "vote_average.gte": max(BASE_FLOOR, min_rating),
        }

        def query(chosen: dict) -> dict[str, Any]:
            return {**best, **self._axes_to_params({**chosen, **kept}, content_type)}

        steps: list[tuple[dict[str, Any], list[int]]] = [(self._axes_to_params(axes, content_type), [4, 5, 6])]
        if "keyword" in axes and favourites:
            cluster = dict.fromkeys([*str(axes["keyword"]).split("-"), *map(str, favourites)])
            genre = {"genre": anchors["genre"]} if "genre" in anchors else {}
            steps.append((query({**genre, "keyword": "-".join(list(cluster)[:5])}), [1, 2, 3]))
        if anchors:
            steps.append((query(anchors), [1, 2, 3, 4]))
        if "genre" in axes:
            steps.append((query({"genre": str(axes["genre"]).split("-")[0]}), [1, 2, 3, 4, 5]))
        return steps


# A mixed theme row is stored as `<type>:<theme id>`: the type whose profile and genre ids the
# theme was built from. Ids without a type are from manifests installed before and are stock.
MIXED_THEME = re.compile(r"^watchly\.theme\.(movie|series):(.+)$")


def _anchors_of(rows: list[RowDefinition]) -> tuple[set[int], set[int]]:
    """Genre and keyword ids a set of rows is built on."""
    genres: set[int] = set()
    keywords: set[int] = set()
    for row in rows:
        for part in row.id.split(".")[2:]:
            _, _, value = part.partition(":")
            ids = {int(v) for v in value[1:].split("-") if v.isdigit()}
            if value.startswith("g"):
                genres |= ids
            elif value.startswith("k"):
                keywords |= ids
    return genres, keywords


def _to_other_type(genres: set[int], from_type: str) -> set[int]:
    table = MOVIE_TO_SERIES_GENRES if from_type == "movie" else SERIES_TO_MOVIE_GENRES
    return {g for genre in genres for g in table.get(genre, [])}


async def mixed_theme_catalogs(
    build_rows: Callable[[str, int, set[int], set[int]], Awaitable[list[RowDefinition]]],
    enabled_movie: bool,
    enabled_series: bool,
    display_at_home: bool,
    row_count: int,
    row_slots: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """`row_count` genre and keyword rows in all, built from movie and series profiles in
    turn so no genre or keyword anchors two of them. Every row is declared under one type
    and a numbered slot; its cards mix both types."""
    types = [t for t, on in (("movie", enabled_movie), ("series", enabled_series)) if on]
    if not types:
        return []
    avoid = {t: (set[int](), set[int]()) for t in types}
    built: dict[str, list[RowDefinition]] = {}
    for position, content_type in enumerate(types):
        count = row_count // len(types) + (position < row_count % len(types))
        try:
            built[content_type] = await build_rows(content_type, count, *avoid[content_type])
        except Exception as e:
            # The other type's rows still stand; this one's slots come up empty and are hidden.
            logger.warning(f"Theme rows for {content_type} unavailable: {type(e).__name__}")
            built[content_type] = []
        genres, keywords = _anchors_of(built[content_type])
        other = OTHER_TYPE[content_type]
        if other in avoid:
            avoid[other][0].update(_to_other_type(genres, content_type))
            avoid[other][1].update(keywords)

    home_type = types[0]
    extra = DISCOVER_ONLY_EXTRA if not display_at_home else []
    rows = [
        (t, row) for group in itertools.zip_longest(*(built[t] for t in types)) for t, row in zip(types, group) if row
    ]
    catalogs = []
    for slot, (content_type, row) in enumerate(rows[:row_count], start=1):
        row_slots.setdefault(home_type, {})[f"theme.{slot}"] = f"{content_type}:{row.id.removeprefix('watchly.theme.')}"
        catalogs.append({"type": home_type, "id": f"watchly.theme.{slot}", "name": row.title, "extra": extra})
    if settings.FIXED_ROW_SLOTS:
        catalogs = fill_slots(catalogs, "watchly.theme.", {home_type: row_count}, "Genre & keyword picks", extra)
    return catalogs


async def mixed_theme_recommendations(
    service: ThemeBasedService,
    catalog_id: str,
    types: list[str],
    states: dict[str, TypeState],
    claimed: dict[str, set[int]],
    limit: int,
) -> list[dict[str, Any]]:
    """The theme's own type from the theme engine, alternating with the other type's
    titles that match the same genres (mapped across), keywords and country."""
    native, rest = MIXED_THEME.match(catalog_id).groups()
    theme_id = f"watchly.theme.{rest}"

    async def build(content_type: str, state: TypeState, share: int) -> list[dict[str, Any]]:
        if content_type == native:
            return await service.get_recommendations_for_theme(theme_id, native, state[0], state[1], state[2], share)
        return await _cross_theme(service, theme_id, native, content_type, state, share)

    return await blend([native] + [t for t in types if t != native], states, claimed, limit, build)


async def _cross_theme(
    service: ThemeBasedService, theme_id: str, native: str, content_type: str, state: TypeState, count: int
) -> list[dict[str, Any]]:
    anchors, flavors, fallbacks = service._parse_theme_id(theme_id)
    axes = {name: value for name, value in {**anchors, **flavors, **fallbacks}.items() if name != "runtime"}
    if "genre" in axes:
        table = MOVIE_TO_SERIES_GENRES if native == "movie" else SERIES_TO_MOVIE_GENRES
        mapped = dict.fromkeys(g for v in str(axes["genre"]).split("-") for g in table.get(int(v), []))
        if mapped:
            axes["genre"] = "-".join(map(str, mapped))
        else:
            del axes["genre"]
    if not axes:
        return []

    profile, watched_tmdb, watched_imdb = state
    excluded = RecommendationFiltering.get_excluded_genre_ids(service.user_settings, content_type)
    found: dict[int, dict[str, Any]] = {}
    # Keyword last: a theme this narrow can have no partner in the other type.
    for drop_keyword in (False, True):
        chosen = {k: v for k, v in axes.items() if not (drop_keyword and k == "keyword")}
        params = {
            "sort_by": "vote_average.desc",
            "vote_count.gte": 300,
            **service._axes_to_params(chosen, content_type),
        }
        for item in await service._fetch_discover_candidates(content_type, params, [1, 2, 3]):
            found.setdefault(item["id"], item)
        if len(found) >= count * 2:
            break

    unique = filter_items_by_settings(list(found.values()), service.user_settings)
    usable = [i for i in filter_by_genres(unique, watched_tmdb, excluded) if clears_floor(i, BASE_FLOOR)]
    mtype = content_type_to_mtype(content_type)
    usable.sort(key=lambda i: quality(i, mtype), reverse=True)
    enriched = await RecommendationMetadata.fetch_batch(
        service.tmdb_service, usable[: math.ceil(count * 1.5)], content_type, user_settings=service.user_settings
    )
    return filter_watched_by_imdb(enriched, watched_imdb)[:count]
