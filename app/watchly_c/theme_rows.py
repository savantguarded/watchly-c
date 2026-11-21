import asyncio
import itertools
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.constants import DISCOVER_ONLY_EXTRA
from app.models.profile import TasteProfile
from app.services.recommendation.filtering import (
    RecommendationFiltering,
    filter_by_genres,
    filter_items_by_settings,
    filter_watched_by_imdb,
)
from app.services.recommendation.metadata import RecommendationMetadata
from app.services.recommendation.scoring import RecommendationScoring
from app.services.recommendation.theme_based import ThemeBasedService
from app.services.recommendation.utils import content_type_to_mtype
from app.services.row_generator import RowDefinition
from app.watchly_c.fit import keyword_ids
from app.watchly_c.mixed_context import TypeState, blend
from app.watchly_c.mixed_rows import MOVIE_TO_SERIES_GENRES, OTHER_TYPE, SERIES_TO_MOVIE_GENRES
from app.watchly_c.relevance import BASE_FLOOR, clears_floor, language_ok, quality
from app.watchly_c.row_slots import fill_slots
from app.watchly_c.seeds import today

# Each discover query is read in two orders, so a row draws on the well-known titles of
# its theme and the best-rated ones alike.
SORTS = ("vote_count.desc", "vote_average.desc")
PAGES = [1, 2, 3]
# A row picks from this many times its length of the best-scored candidates. The day picks
# the order, so a theme that comes back doesn't come back with the same titles.
CHOICE_DEPTH = 3
# Raised to sharpen the draw toward the better-scored titles while still varying it.
SHARPNESS = 4


@dataclass(frozen=True)
class ThemeAxes:
    """A row's filters. Every genre group must match (a group is one genre, or the genres a
    genre maps to in the other type); stock merged the row's roles into one dict, so a
    "Crime Drama" row lost one of its two genres."""

    genre_groups: tuple[frozenset[int], ...]
    keywords: frozenset[int]
    rest: dict[str, str]

    @classmethod
    def parse(cls, theme_id: str) -> "ThemeAxes":
        genres: list[int] = []
        keywords: set[int] = set()
        rest: dict[str, str] = {}
        for part in theme_id.removeprefix("watchly.theme.").split("."):
            _, _, value = part.partition(":")
            if value.startswith("ct"):
                rest["country"] = value[2:]
            elif value.startswith("cr"):
                continue
            elif value.startswith("g"):
                genres += [int(v) for v in value[1:].split("-") if v.isdigit()]
            elif value.startswith("k"):
                keywords |= {int(v) for v in value[1:].split("-") if v.isdigit()}
            elif value.startswith("y"):
                rest["era"] = value[1:]
            elif value.startswith("r"):
                rest["runtime"] = value[1:]
        return cls(tuple(frozenset({g}) for g in dict.fromkeys(genres)), frozenset(keywords), rest)

    def across(self, native: str) -> "ThemeAxes":
        """The same row for the other type: genres mapped across and no runtime, which
        doesn't carry between films and episodes. A genre with no partner (TMDB series have
        no Horror) is dropped only when a keyword still holds the theme; dropping it from
        "Comedy and Horror" left plain Comedy and filled the row with sitcoms."""
        table = MOVIE_TO_SERIES_GENRES if native == "movie" else SERIES_TO_MOVIE_GENRES
        groups = tuple(frozenset(g for v in group for g in table.get(v, [])) for group in self.genre_groups)
        if not all(groups) and not self.keywords:
            return ThemeAxes((), frozenset(), {})
        return ThemeAxes(
            tuple(g for g in groups if g), self.keywords, {k: v for k, v in self.rest.items() if k != "runtime"}
        )

    def is_empty(self) -> bool:
        return not (self.genre_groups or self.keywords or self.rest)

    def params(self, service: ThemeBasedService, content_type: str, with_runtime: bool = True) -> dict[str, Any]:
        params = service._axes_to_params(
            {k: v for k, v in self.rest.items() if with_runtime or k != "runtime"}, content_type
        )
        single = [str(next(iter(g))) for g in self.genre_groups if len(g) == 1]
        if single:
            params["with_genres"] = ",".join(single)
        elif self.genre_groups:
            params["with_genres"] = "|".join(map(str, sorted(self.genre_groups[0])))
        if self.keywords:
            params["with_keywords"] = "|".join(map(str, sorted(self.keywords)))
        return params

    def matches(self, item: dict[str, Any]) -> bool:
        """Genres from the compact shape; the keyword is the query's own."""
        genres = set(item.get("genre_ids") or ())
        return all(group & genres for group in self.genre_groups)

    def fits(self, item: dict[str, Any]) -> bool:
        """Once details load: the row's keyword is on the title and every genre group is."""
        return (not self.keywords or bool(keyword_ids(item) & self.keywords)) and self.matches(item)


def day_order(items: list[dict[str, Any]], row_key: str, score: Callable[[dict[str, Any]], float]):
    """A weighted draw without replacement, seeded by the row and the day: better-scored
    titles still tend to lead, but which ones lead changes from one day to the next."""
    rng = random.Random(f"{row_key}|{today().isoformat()}")
    keys = {i["id"]: rng.random() ** (1 / max(score(i), 0.01) ** SHARPNESS) for i in items}
    return sorted(items, key=lambda i: keys[i["id"]], reverse=True)


async def theme_picks(
    service: ThemeBasedService,
    axes: ThemeAxes,
    row_key: str,
    content_type: str,
    profile: TasteProfile | None,
    watched_tmdb: set[int],
    watched_imdb: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Titles matching every filter of the row, held to the rating floor. Only the runtime
    is ever relaxed; a row that runs out of titles stays short."""
    excluded = RecommendationFiltering.get_excluded_genre_ids(service.user_settings, content_type)
    mtype = content_type_to_mtype(content_type)
    found: dict[int, dict[str, Any]] = {}
    usable: list[dict[str, Any]] = []
    for with_runtime in (True, False) if "runtime" in axes.rest else (True,):
        base = axes.params(service, content_type, with_runtime)
        min_votes = RecommendationFiltering.get_quality_thresholds(service.user_settings)[1]
        batches = await asyncio.gather(
            *(
                service._fetch_discover_candidates(
                    content_type, {**base, "sort_by": sort, "vote_count.gte": max(200, min_votes)}, PAGES
                )
                for sort in SORTS
            )
        )
        for item in itertools.chain.from_iterable(batches):
            found.setdefault(item["id"], item)
        unique = filter_items_by_settings(list(found.values()), service.user_settings)
        usable = [
            i
            for i in filter_by_genres(unique, watched_tmdb, excluded)
            if clears_floor(i, BASE_FLOOR) and axes.matches(i)
            # English unless the row is about a country; a row of subtitled titles was the complaint.
            and ("country" in axes.rest or language_ok(i, None))
        ]
        if len(usable) >= limit * CHOICE_DEPTH:
            break

    def score(item: dict[str, Any]) -> float:
        fit = (
            RecommendationScoring.calculate_final_score(item=item, profile=profile, scorer=service.scorer, mtype=mtype)
            if profile
            else 0.5
        )
        return 0.5 * quality(item, mtype) + 0.5 * min(fit, 1.0)

    best = sorted(usable, key=score, reverse=True)[: limit * CHOICE_DEPTH]
    chosen = day_order(best, f"{row_key}|{content_type}", score)
    enriched = await RecommendationMetadata.fetch_batch(
        service.tmdb_service, chosen[: math.ceil(limit * 1.4)], content_type, user_settings=service.user_settings
    )
    if settings.FIT_GATE:
        enriched = [i for i in enriched if axes.fits(i)]
    return filter_watched_by_imdb(enriched, watched_imdb)[:limit]


class WideThemeService(ThemeBasedService):
    """Genre and keyword rows that keep to every filter in their name and change their
    titles from day to day.

    Stock widened a short row to each axis on its own, so a "Murder" crime row filled with
    any crime film, and always served the same top-scored titles for the same theme.
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
        axes = ThemeAxes.parse(theme_id)
        if axes.is_empty():
            return []
        return await theme_picks(
            self, axes, theme_id, content_type, profile, watched_tmdb or set(), watched_imdb or set(), limit
        )


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
    axes = ThemeAxes.parse(theme_id).across(native)
    if axes.is_empty():
        return []
    return await theme_picks(service, axes, theme_id, content_type, *state, count)
