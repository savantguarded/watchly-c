import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from loguru import logger

from app.core.constants import CONTEXT_LOG_KEY, DISCOVER_ONLY_EXTRA
from app.core.settings import CatalogConfig, UserSettings
from app.models.library import LibraryCollection, StremioLibraryItem
from app.models.profile import TasteProfile
from app.services.profile.scorer import ProfileScorer
from app.services.recommendation.filtering import (
    RecommendationFiltering,
    filter_by_genres,
    filter_items_by_settings,
    filter_watched_by_imdb,
)
from app.services.recommendation.metadata import RecommendationMetadata
from app.services.recommendation.scoring import RecommendationScoring
from app.services.recommendation.utils import content_type_to_mtype, resolve_tmdb_id
from app.services.redis_service import redis_service
from app.services.tmdb.service import TMDBService
from app.watchly_c.relevance import BASE_FLOOR, MIN_VOTES, clears_floor, quality, row_size
from app.watchly_c.row_slots import CONTEXT_SLOTS, fill_slots
from app.watchly_c.seeds import choose_seeds, today
from app.watchly_c.weather import rains_today

CONTEXT_CONFIG_ID = "watchly.ctx"
CONTEXT_PREFIX = "watchly.ctx."
LOG_TTL_SECONDS = 45 * 86400
# Widening levels: more pages and a lower vote floor each time, only while a row is short.
LEVELS = 3
ANNIVERSARIES = (5, 10, 15, 20, 25, 30, 35, 40)
ON_THIS_DAY_WINDOW_DAYS = (3, 10, 21)

Kind = Literal["season", "moment", "evergreen"]
# One row for the season, one for the moment, one that rotates through everything else.
SLOT_KINDS: tuple[Kind, ...] = ("season", "moment", "evergreen")


@dataclass(frozen=True)
class Look:
    """One TMDB discover query. `keywords` are names, resolved to ids and matched on any."""

    genres: str | None = None
    keywords: tuple[str, ...] = ()
    without_genres: str | None = None
    countries: str | None = None
    runtime: tuple[int | None, int | None] = (None, None)
    sort: str = "vote_average.desc"
    votes: int = 500
    rating: float | None = None
    recent_days: int | None = None
    dates: tuple[date, date] | None = None
    pages: int = 3
    # Rows are English by default; the rows about somewhere else switch it off.
    language: str | None = "en"
    extra: dict[str, Any] = field(default_factory=dict)

    def params(self, mtype: str, level: int, floor: float, day: date, keyword_ids: list[int]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "sort_by": self.sort,
            "vote_count.gte": max(self.votes // (1 + level), 10),
            "vote_average.gte": self.rating or floor,
        }
        if self.genres:
            params["with_genres"] = self.genres
        if self.without_genres:
            params["without_genres"] = self.without_genres
        if keyword_ids:
            params["with_keywords"] = "|".join(map(str, keyword_ids))
        if self.language:
            params["with_original_language"] = self.language
        if self.countries:
            params["with_origin_country"] = self.countries
        shortest, longest = self.runtime
        if shortest:
            params["with_runtime.gte"] = shortest
        if longest:
            params["with_runtime.lte"] = longest
        window = self.dates or ((day - timedelta(days=self.recent_days), day) if self.recent_days else None)
        if window:
            prefix = "primary_release_date" if mtype == "movie" else "first_air_date"
            params[f"{prefix}.gte"], params[f"{prefix}.lte"] = (d.isoformat() for d in window)
        return {**params, **self.extra}


@dataclass(frozen=True)
class Plan:
    mtype: str
    day: date
    arg: str | None
    level: int
    profile: TasteProfile | None


@dataclass(frozen=True)
class Spec:
    key: str
    kind: Kind
    name: str
    looks: dict[str, tuple[Look, ...]] = field(default_factory=dict)
    # For rows whose queries depend on the day, the profile or a seed.
    types: tuple[str, ...] = ("movie", "series")
    build: Callable[["ContextRowService", Plan], Any] | None = None
    # (month, day) to (month, day), inclusive; None means any day.
    window: tuple[tuple[int, int], tuple[int, int]] | None = None
    floor: float = BASE_FLOOR
    min_votes: int = MIN_VOTES
    max_popularity: float | None = None


async def _on_this_day(service: "ContextRowService", plan: Plan) -> tuple[Look, ...]:
    """Titles released around today's date 5, 10, 15 ... 40 years ago, most voted first."""
    reach = timedelta(days=ON_THIS_DAY_WINDOW_DAYS[plan.level])
    looks = []
    for years in ANNIVERSARIES:
        # Feb 29 has no anniversary in most years; the 28th is close enough for a window.
        anniversary = date(plan.day.year - years, plan.day.month, min(plan.day.day, 28))
        looks.append(Look(dates=(anniversary - reach, anniversary + reach), sort="vote_count.desc", votes=300, pages=1))
    return tuple(looks)


async def _decade(service: "ContextRowService", plan: Plan) -> tuple[Look, ...]:
    start = int(plan.arg or 1990)
    dates = (date(start, 1, 1), date(start + 9, 12, 31))
    rated = (1500, 7.2) if plan.mtype == "movie" else (400, 7.3)
    return (Look(dates=dates, votes=rated[0], rating=rated[1]),)


async def _hidden_gems(service: "ContextRowService", plan: Plan) -> tuple[Look, ...]:
    genres = "|".join(str(g) for g, _ in plan.profile.get_top_genres(limit=3)) if plan.profile else None
    # Recent shows are rated by few voters while everyone is watching them, so a vote
    # ceiling alone lets a hit through; the date and the popularity cap in the spec don't.
    old = (date(1950, 1, 1), plan.day - timedelta(days=730))
    return (Look(genres=genres or None, votes=150, rating=7.4, dates=old, extra={"vote_count.lte": 3000}),)


async def _people(service: "ContextRowService", plan: Plan) -> tuple[Look, ...]:
    """The seed's director, and its three leading cast, whichever films they made."""
    tmdb_id = await resolve_tmdb_id(plan.arg or "", service.tmdb_service)
    if not tmdb_id:
        return ()
    details = await service.tmdb_service.get_movie_details(tmdb_id)
    credits = details.get("credits") or {}
    directors = [c["id"] for c in credits.get("crew", []) if c.get("job") == "Director"][:1]
    cast = [c["id"] for c in credits.get("cast", [])[:3]]
    looks = []
    if directors:
        looks.append(Look(votes=150, extra={"with_crew": directors[0]}))
    if cast:
        looks.append(Look(votes=300, extra={"with_cast": "|".join(map(str, cast))}))
    return tuple(looks)


SPOOKY_GENTLE = ("halloween", "witch", "ghost", "monster", "haunted house")
HOLIDAY = ("christmas", "christmas tree", "santa claus", "holiday", "winter")
AFRICA = "NG|GH|ZA|KE|EG|MA|SN|TZ|UG|CI|CM|DZ|TN|ET"
SIX_YEARS = 6 * 365

SPECS: tuple[Spec, ...] = (
    Spec(
        "spooky",
        "season",
        "Spooky Season",
        {
            "movie": (
                Look(genres="27", votes=800),
                Look(
                    genres="27|53|9648",
                    keywords=("halloween", "haunted house", "ghost", "supernatural", "slasher"),
                    votes=400,
                ),
            ),
            "series": (
                Look(keywords=("horror", "supernatural", "ghost", "haunted house", "witch"), votes=300),
                Look(genres="9648,10765", votes=300),
            ),
        },
        window=((10, 1), (11, 1)),
    ),
    Spec(
        "spooky_gentle",
        "season",
        "Spooky, Not Scary",
        {
            "movie": (Look(genres="10751|16|35|14", keywords=SPOOKY_GENTLE, without_genres="27", votes=300),),
            "series": (Look(genres="10751|16|35|10765|10762", keywords=SPOOKY_GENTLE, votes=150),),
        },
        window=((10, 1), (11, 1)),
    ),
    Spec(
        "holiday",
        "season",
        "Holiday Season",
        {"movie": (Look(keywords=HOLIDAY, votes=200),), "series": (Look(keywords=HOLIDAY, votes=100),)},
        window=((12, 1), (12, 31)),
    ),
    Spec(
        "valentines",
        "season",
        "Valentine's Picks",
        {
            "movie": (
                Look(genres="10749", votes=800),
                Look(genres="35|18", keywords=("valentine's day", "love", "romance"), votes=500),
            ),
            "series": (Look(genres="18|35", keywords=("romance", "love", "relationship"), votes=300),),
        },
        window=((2, 1), (2, 14)),
    ),
    Spec(
        "awards",
        "season",
        "Awards Season",
        {
            "movie": (Look(genres="18|36|10752|10402|53", recent_days=540, votes=1000, rating=7.2),),
            "series": (Look(genres="18|10768|80|9648", recent_days=540, votes=400, rating=7.4),),
        },
        window=((1, 1), (3, 15)),
        floor=7.0,
    ),
    Spec(
        "summer",
        "season",
        "Summer Blockbusters",
        {
            "movie": (Look(genres="28|12|878", sort="revenue.desc", recent_days=SIX_YEARS, votes=2000),),
            "series": (
                Look(genres="10759|10765", sort="popularity.desc", recent_days=SIX_YEARS, votes=1000, rating=7.0),
            ),
        },
        window=((6, 1), (8, 31)),
    ),
    Spec(
        "african",
        "season",
        "African Stories",
        {
            "movie": (Look(countries=AFRICA, sort="popularity.desc", votes=30, pages=4, language=None),),
            "series": (Look(countries=AFRICA, sort="popularity.desc", votes=30, pages=4, language=None),),
        },
        window=((10, 1), (10, 31)),
        # TMDB holds few votes for most African titles, so a vote floor of 100 would empty the row.
        min_votes=15,
    ),
    Spec(
        "weekend",
        "moment",
        "{weekday} Night",
        {
            "movie": (Look(genres="28|35|12", runtime=(90, 135), sort="popularity.desc", votes=1500, rating=6.8),),
            "series": (Look(genres="35|10759|80", sort="popularity.desc", votes=800, rating=7.0),),
        },
    ),
    Spec(
        "sunday",
        "moment",
        "Sunday Slow",
        {
            "movie": (Look(genres="18|10749|10402|36", runtime=(100, None), votes=1000, rating=7.2),),
            "series": (Look(genres="18", votes=500, rating=7.5),),
        },
    ),
    Spec(
        "rainy",
        "moment",
        "Rainy Day Comfort",
        {
            "movie": (Look(genres="35|10749|18", runtime=(None, 125), votes=1000, rating=7.0),),
            "series": (Look(genres="35|18", votes=500, rating=7.3),),
        },
    ),
    Spec("on_this_day", "moment", "On This Day, {day} {month}", types=("movie",), build=_on_this_day),
    Spec(
        "late_night",
        "evergreen",
        "Late Night Watch",
        {
            "movie": (Look(genres="53|9648|35|878", runtime=(70, 100), votes=800, rating=6.8),),
            "series": (Look(genres="35|80|9648", runtime=(None, 35), votes=500, rating=7.0),),
        },
    ),
    Spec("hidden_gems", "evergreen", "Hidden Gems for You", build=_hidden_gems, max_popularity=40.0),
    Spec("people", "evergreen", "From the People Behind {person}", types=("movie",), build=_people),
    Spec("decade", "evergreen", "Time Machine: the {decade}s", build=_decade),
    Spec(
        "short_binges",
        "evergreen",
        "Short Binges",
        {"series": (Look(votes=200, rating=7.0, extra={"with_type": 2}),)},
        types=("series",),
    ),
)
SPEC_BY_KEY = {s.key: s for s in SPECS}


@dataclass(frozen=True)
class Today:
    day: date
    rain: bool
    has_people_seed: bool


def is_eligible(spec: Spec, today_: Today) -> bool:
    if spec.window:
        start, end = spec.window
        if not start <= (today_.day.month, today_.day.day) <= end:
            return False
    if spec.key == "weekend":
        return today_.day.weekday() in (4, 5)
    if spec.key == "sunday":
        return today_.day.weekday() == 6
    if spec.key == "rainy":
        return today_.rain
    if spec.key == "people":
        return today_.has_people_seed
    return True


def choose_specs(content_type: str, today_: Today, shown: dict[str, str], rng: random.Random) -> list[Spec]:
    """One spec per slot, in slot order. Within a kind the one shown longest ago goes
    first, so every row that fits the day gets its turn. A slot with nothing that fits
    (no season, a dry weekday) is filled from the rotating kind instead."""
    chosen: list[Spec] = []
    for kind in SLOT_KINDS:
        available = [
            s
            for s in SPECS
            if content_type in s.types and is_eligible(s, today_) and s not in chosen and s.kind == kind
        ] or [
            s
            for s in SPECS
            if content_type in s.types and is_eligible(s, today_) and s not in chosen and s.kind == "evergreen"
        ]
        if available:
            chosen.append(min(available, key=lambda s: (shown.get(s.key, ""), rng.random())))
    return chosen


def _title(item: StremioLibraryItem) -> str:
    return (item.name or "").strip() or "this title"


async def context_catalogs(
    token: str | None,
    config: CatalogConfig | None,
    library: LibraryCollection,
    last_watched: Callable[[StremioLibraryItem], Any],
    year_min: int,
    row_slots: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Today's seasonal and contextual rows, as `watchly.ctx.1..3` slots per type.

    What a slot shows changes with the day, its id doesn't, so Nuvio keeps each where
    the user put it. The first build of a day records its picks and later builds reuse
    them, the way seeds are kept: a row's name can't change under it mid-day.
    """
    if not config or not config.enabled:
        return []
    types = [t for t, on in (("movie", config.enabled_movie), ("series", config.enabled_series)) if on]
    if not types:
        return []

    day = today()
    raw = await redis_service.get(CONTEXT_LOG_KEY.format(token=token)) if token else None
    log: dict[str, Any] = json.loads(raw) if raw else {}
    if log.get("day") != day.isoformat():
        log = {"day": day.isoformat(), "picks": {}, "shown": log.get("shown", {})}

    missing = [t for t in types if t not in log["picks"]]
    if missing:
        movie_pool = [i for i in library.loved + library.liked + library.watched if i.type == "movie"]
        today_ = Today(day, await rains_today(day), bool(movie_pool))
        for content_type in missing:
            shown = log["shown"].setdefault(content_type, {})
            rng = random.Random(f"{token}:{day.isoformat()}:{content_type}")
            picks = []
            for spec in choose_specs(content_type, today_, shown, rng):
                shown[spec.key] = day.isoformat()
                picks.append(await _pick(token, spec, content_type, day, movie_pool, last_watched, year_min))
            log["picks"][content_type] = picks
        if token:
            await redis_service.set(CONTEXT_LOG_KEY.format(token=token), json.dumps(log), LOG_TTL_SECONDS)

    extra = [] if config.display_at_home else DISCOVER_ONLY_EXTRA
    catalogs = []
    for content_type in types:
        for slot, pick in enumerate(log["picks"][content_type], start=1):
            row_slots.setdefault(content_type, {})[f"ctx.{slot}"] = pick["definition"]
            catalogs.append(
                {"type": content_type, "id": f"{CONTEXT_PREFIX}{slot}", "name": pick["name"], "extra": extra}
            )
    return fill_slots(
        catalogs, CONTEXT_PREFIX, {t: CONTEXT_SLOTS for t in types}, config.name or "Seasonal & contextual", extra
    )


async def _pick(
    token: str | None,
    spec: Spec,
    content_type: str,
    day: date,
    movie_pool: list[StremioLibraryItem],
    last_watched: Callable[[StremioLibraryItem], Any],
    year_min: int,
) -> dict[str, str]:
    """A slot's definition (`key` or `key:arg`) and the name it is shown under."""
    fields = {"weekday": f"{day:%A}", "day": str(day.day), "month": f"{day:%b}"}
    arg = None
    if spec.key == "people":
        seed = (await choose_seeds(token, movie_pool, 1, "people", last_watched))[0]
        arg, fields["person"] = seed.id, _title(seed)
    elif spec.key == "decade":
        decades = list(range(max(1970, year_min // 10 * 10), day.year // 10 * 10 + 1, 10))
        arg = str(decades[day.toordinal() % len(decades)])
        fields["decade"] = arg
    return {"definition": f"{spec.key}:{arg}" if arg else spec.key, "name": spec.name.format(**fields)}


class ContextRowService:
    """Builds one seasonal or contextual row from its definition."""

    def __init__(self, tmdb_service: TMDBService, user_settings: UserSettings):
        self.tmdb_service = tmdb_service
        self.user_settings = user_settings
        self.scorer = ProfileScorer()

    async def get_recommendations(
        self,
        catalog_id: str,
        content_type: str,
        profile: TasteProfile | None,
        watched_tmdb: set[int],
        watched_imdb: set[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        key, _, arg = catalog_id.removeprefix(CONTEXT_PREFIX).partition(":")
        spec = SPEC_BY_KEY[key]
        mtype = content_type_to_mtype(content_type)
        day = today()
        excluded = RecommendationFiltering.get_excluded_genre_ids(self.user_settings, content_type)

        found: dict[int, dict[str, Any]] = {}
        usable: list[dict[str, Any]] = []
        for level in range(LEVELS):
            plan = Plan(mtype, day, arg or None, level, profile)
            for look in await self._looks(spec, content_type, plan):
                keyword_ids = await self._keyword_ids(look.keywords)
                if look.keywords and not keyword_ids:
                    continue
                params = look.params(mtype, level, spec.floor, day, keyword_ids)
                # Single-page looks (On This Day's windows) widen by date instead.
                pages = range(1, look.pages + (2 * level if look.pages > 1 else 0) + 1)
                results = await asyncio.gather(
                    *(self.tmdb_service.get_discover(mtype, page=p, **params) for p in pages), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        logger.warning(f"Discover for {key} failed: {type(result).__name__}")
                        continue
                    for item in result.get("results", []):
                        if item.get("id"):
                            found.setdefault(item["id"], item)
            unique = filter_items_by_settings(list(found.values()), self.user_settings, apply_quality_band=False)
            usable = [
                i
                for i in filter_by_genres(unique, watched_tmdb, excluded)
                if clears_floor(i, spec.floor, spec.min_votes)
                and (spec.max_popularity is None or (i.get("popularity") or 0) <= spec.max_popularity)
            ]
            if len(usable) >= row_size(limit):
                break

        usable.sort(key=lambda i: self._score(i, profile, mtype), reverse=True)
        enriched = await RecommendationMetadata.fetch_batch(
            self.tmdb_service, usable[: row_size(limit)], content_type, user_settings=self.user_settings
        )
        return filter_watched_by_imdb(enriched, watched_imdb)

    async def _looks(self, spec: Spec, content_type: str, plan: Plan) -> tuple[Look, ...]:
        if spec.build:
            return await spec.build(self, plan)
        return spec.looks[content_type]

    def _score(self, item: dict[str, Any], profile: TasteProfile | None, mtype: str) -> float:
        # Taste re-ranks within the theme, so a horror fan's Spooky Season leads with theirs.
        if profile:
            return RecommendationScoring.calculate_final_score(item, profile, self.scorer, mtype)
        return quality(item, mtype)

    async def _keyword_ids(self, names: tuple[str, ...]) -> list[int]:
        results = await asyncio.gather(*(self.tmdb_service.search_keywords(n) for n in names), return_exceptions=True)
        ids = []
        for name, result in zip(names, results):
            if isinstance(result, BaseException) or not result.get("results"):
                continue
            exact = next((k for k in result["results"] if k["name"].lower() == name), result["results"][0])
            ids.append(exact["id"])
        return ids
