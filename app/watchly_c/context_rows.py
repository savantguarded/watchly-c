import asyncio
import itertools
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from loguru import logger

from app.core.constants import CONTEXT_LOG_KEY, DEFAULT_CONCURRENCY_LIMIT, DISCOVER_ONLY_EXTRA
from app.core.settings import DEFAULT_YEAR_MIN, CatalogConfig, UserSettings
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
from app.watchly_c.mixed_context import TypeState, blend
from app.watchly_c.relevance import BASE_FLOOR, MIN_VOTES, clears_floor, quality, row_size
from app.watchly_c.row_slots import fill_slots
from app.watchly_c.seeds import choose_seeds, today
from app.watchly_c.weather import rains_today

CONTEXT_CONFIG_ID = "watchly.ctx"
CONTEXT_PREFIX = "watchly.ctx."
LOG_TTL_SECONDS = 45 * 86400
# Bumped when the rules for choosing a day's rows change, so a day already chosen is chosen again.
LOG_VERSION = 5
# A row that ran within this many days sits out, so a season with one row (Family Feasts
# through late November) doesn't take the season slot every day of its window.
REPEAT_AFTER_DAYS = 7
# Widening levels: more pages and a lower vote floor each time, only while a row is short.
LEVELS = 3
FIRST_ON_THIS_DAY_YEAR = 1960
# Days either side of today's date: the exact day first, nearby days only while the row is short.
ON_THIS_DAY_REACH_DAYS = (0, 1, 2)

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
    # Dates the title opened in US cinemas. TMDB's own release date is the earliest
    # anywhere, often a festival premiere weeks before the real opening.
    theatrical: bool = False
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
            if self.theatrical:
                prefix = "release_date"
                params["region"], params["with_release_type"] = "US", "2|3"
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
    # Always the first row of a type that can show it.
    pinned: bool = False
    max_popularity: float | None = None
    # Titles found at a tighter level come before ones that only a wider level found.
    nearest_first: bool = False
    # TMDB holds few votes for these titles, so the popularity setting's vote floor would empty the row.
    ignores_popularity: bool = False
    # Other names for the same row; the day picks which one it goes by.
    alt_names: tuple[str, ...] = ()


async def _on_this_day(service: "ContextRowService", plan: Plan) -> tuple[Look, ...]:
    """Titles that opened in US cinemas on today's date in any year since 1960, most voted first."""
    reach = timedelta(days=ON_THIS_DAY_REACH_DAYS[plan.level])
    looks = []
    for year in range(FIRST_ON_THIS_DAY_YEAR, plan.day.year):
        try:
            anniversary = plan.day.replace(year=year)
        except ValueError:
            anniversary = date(year, 2, 28)
        looks.append(
            Look(
                dates=(anniversary - reach, anniversary + reach),
                sort="vote_count.desc",
                votes=300,
                pages=1,
                theatrical=True,
            )
        )
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
        alt_names=("Halloween Watchlist", "Fright Night", "Creeping Dread"),
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
        alt_names=("Friendly Frights", "Scary Lite", "Halloween for the Whole Family"),
    ),
    Spec(
        "holiday",
        "season",
        "Holiday Season",
        {"movie": (Look(keywords=HOLIDAY, votes=200),), "series": (Look(keywords=HOLIDAY, votes=100),)},
        window=((12, 1), (12, 31)),
        alt_names=("Festive Season", "Holiday Watchlist", "Cosy Christmas Picks"),
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
        alt_names=("Love Is in the Air", "Date Night Picks", "Hearts and Flowers"),
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
        alt_names=("Awards Contenders", "Prestige Picks", "The Ones Everyone Is Talking About"),
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
        alt_names=("Big Summer Movies", "Popcorn Season", "Summer Hits"),
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
        ignores_popularity=True,
        alt_names=("Stories From Africa", "Across the Continent", "Africa on Screen"),
    ),
    Spec(
        "weekend",
        "moment",
        "{weekday} Night",
        {
            "movie": (Look(genres="28|35|12", runtime=(90, 135), sort="popularity.desc", votes=1500, rating=6.8),),
            "series": (Look(genres="35|10759|80", sort="popularity.desc", votes=800, rating=7.0),),
        },
        alt_names=("{weekday} Night In", "Your {weekday} Night", "{weekday} Night Watch"),
    ),
    Spec(
        "sunday",
        "moment",
        "Sunday Slow",
        {
            "movie": (Look(genres="18|10749|10402|36", runtime=(100, None), votes=1000, rating=7.2),),
            "series": (Look(genres="18", votes=500, rating=7.5),),
        },
        alt_names=("Slow Sunday", "Lazy Sunday", "Sunday Afternoon Watch"),
    ),
    Spec(
        "rainy",
        "moment",
        "Rainy Day Comfort",
        {
            "movie": (Look(genres="35|10749|18", runtime=(None, 125), votes=1000, rating=7.0),),
            "series": (Look(genres="35|18", votes=500, rating=7.3),),
        },
        alt_names=("Rainy Day Picks", "Cosy Rain Day", "Stay In, It's Pouring"),
    ),
    Spec(
        "on_this_day",
        "moment",
        "On This Day, {day} {month}",
        types=("movie",),
        build=_on_this_day,
        pinned=True,
        nearest_first=True,
        alt_names=("{day} {month} Through the Years", "Opened on {day} {month}", "This Day at the Cinema"),
    ),
    Spec(
        "late_night",
        "evergreen",
        "Late Night Watch",
        {
            "movie": (Look(genres="53|9648|35|878", runtime=(70, 100), votes=800, rating=6.8),),
            "series": (Look(genres="35|80|9648", runtime=(None, 35), votes=500, rating=7.0),),
        },
        alt_names=("After Midnight", "Can't Sleep?", "One More Before Bed"),
    ),
    Spec(
        "hidden_gems",
        "evergreen",
        "Hidden Gems for You",
        build=_hidden_gems,
        max_popularity=40.0,
        alt_names=("Overlooked and Underrated", "Gems You May Have Missed", "Under the Radar"),
    ),
    Spec(
        "people",
        "evergreen",
        "From the People Behind {person}",
        types=("movie",),
        build=_people,
        alt_names=("More From the Makers of {person}", "Same Makers as {person}", "Behind {person}"),
    ),
    Spec(
        "decade",
        "evergreen",
        "Time Machine: the {decade}s",
        build=_decade,
        alt_names=("Back to the {decade}s", "The {decade}s Revisited", "{decade}s Throwback"),
    ),
    Spec(
        "short_binges",
        "evergreen",
        "Short Binges",
        {"series": (Look(votes=200, rating=7.0, extra={"with_type": 2}),)},
        types=("series",),
        alt_names=("Quick Binges", "Binge in a Weekend", "Minis Worth Watching"),
    ),
    Spec(
        "monday",
        "moment",
        "Monday Motivation",
        {
            "movie": (
                Look(keywords=("underdog", "sports", "inspirational", "triumph"), votes=400, rating=7.0),
                Look(genres="18", keywords=("underdog", "coach"), votes=300, rating=7.2),
            ),
            "series": (Look(keywords=("underdog", "sports", "inspirational"), votes=150, rating=7.3),),
        },
        alt_names=("Monday Boost", "Start the Week Strong", "Monday Fuel"),
    ),
    Spec(
        "midweek",
        "moment",
        "Midweek Pick-Me-Up",
        {
            "movie": (Look(genres="35", runtime=(None, 110), votes=1000, rating=7.0),),
            "series": (Look(genres="35", runtime=(None, 35), votes=400, rating=7.5),),
        },
        alt_names=("Midweek Lift", "Hump Day Laughs", "Wednesday Pick-Me-Up"),
    ),
    Spec(
        "thursday",
        "moment",
        "Thursday Thrills",
        {
            "movie": (Look(genres="53|9648|80", votes=1000, rating=7.0),),
            "series": (Look(genres="80|9648", votes=500, rating=7.5),),
        },
        alt_names=("Thursday Suspense", "Nearly Friday Thrills", "Thursday Night Tension"),
    ),
    Spec(
        "payday",
        "moment",
        "Payday Treat",
        {
            "movie": (
                Look(genres="28|12|878|14", sort="popularity.desc", recent_days=SIX_YEARS, votes=2000, rating=6.8),
            ),
            "series": (
                Look(genres="10759|10765", sort="popularity.desc", recent_days=SIX_YEARS, votes=800, rating=7.2),
            ),
        },
        alt_names=("Treat Yourself", "Payday Watchlist", "You Earned This"),
    ),
    Spec(
        "back_to_school",
        "season",
        "Back to School",
        {
            "movie": (Look(genres="35|18|10749", keywords=("high school", "teacher", "college"), votes=400),),
            "series": (Look(keywords=("high school", "teacher", "college"), votes=200),),
        },
        window=((8, 25), (9, 20)),
        alt_names=("Back in Class", "School Days", "Term Time"),
    ),
    Spec(
        "detty_december",
        "season",
        "Detty December",
        {
            "movie": (Look(genres="35|10402|10749", votes=500, rating=6.8),),
            "series": (Look(genres="35", votes=200, rating=7.2),),
        },
        window=((12, 10), (12, 31)),
        alt_names=("December in Lagos", "Detty December Vibes", "December to Remember"),
    ),
    Spec(
        "mind_benders",
        "evergreen",
        "Mind Benders",
        {
            "movie": (
                Look(
                    keywords=("twist ending", "nonlinear timeline", "time loop", "parallel universe", "dream"),
                    votes=800,
                    rating=7.2,
                ),
            ),
            "series": (
                Look(keywords=("twist ending", "time travel", "parallel universe", "mystery"), votes=200, rating=7.4),
            ),
        },
        alt_names=("Brain Twisters", "Wait, What?", "Mind-Bending Stories"),
    ),
    Spec(
        "true_stories",
        "evergreen",
        "True Stories",
        {
            "movie": (Look(keywords=("based on true story", "biography", "true crime"), votes=800, rating=7.2),),
            "series": (Look(keywords=("based on true story", "true crime"), votes=200, rating=7.3),),
        },
        alt_names=("Based on Real Life", "Real Events, Real People", "Truth on Screen"),
    ),
    Spec(
        "feel_good",
        "evergreen",
        "Feel-Good Favourites",
        {
            "movie": (Look(genres="35|10751|10749", runtime=(None, 120), votes=800, rating=7.0),),
            "series": (Look(genres="35|10751", votes=300, rating=7.3),),
        },
        alt_names=("Feel-Good Picks", "Smiles Guaranteed", "Pure Good Vibes"),
    ),
    Spec(
        "heists",
        "evergreen",
        "Heists and Cons",
        {
            "movie": (Look(keywords=("heist", "con artist", "robbery"), votes=500, rating=6.9),),
            "series": (Look(keywords=("heist", "con artist", "robbery"), votes=150, rating=7.2),),
        },
        alt_names=("Capers and Heists", "The Big Score", "Pulling Off the Job"),
    ),
    Spec(
        "classics",
        "evergreen",
        "Certified Classics",
        {"movie": (Look(dates=(date(1950, 1, 1), date(2005, 12, 31)), votes=4000, rating=7.8),)},
        types=("movie",),
        alt_names=("Timeless Classics", "Classics Worth Another Look", "The Greats"),
    ),
    Spec(
        "finished_series",
        "evergreen",
        "Finished and Worth the Binge",
        {"series": (Look(votes=1000, rating=8.0, extra={"with_status": "3"}),)},
        types=("series",),
        alt_names=("Complete Series to Binge", "Whole Story, Start to Finish", "Binge-Ready Series"),
    ),
    Spec(
        "short_and_sweet",
        "evergreen",
        "Short and Sweet",
        {"movie": (Look(runtime=(60, 95), votes=800, rating=7.0),)},
        types=("movie",),
        alt_names=("Quick Movie Night", "Home Before Bedtime", "Ninety Minutes or Less"),
    ),
    Spec(
        "epics",
        "evergreen",
        "Epics Worth the Time",
        {"movie": (Look(runtime=(150, None), votes=2000, rating=7.6),)},
        types=("movie",),
        alt_names=("Big Epics", "Settle In for These", "Clear Your Evening"),
    ),
    Spec(
        "animation_grown_up",
        "evergreen",
        "Animation Beyond Kids",
        {
            "movie": (Look(genres="16", without_genres="10751", votes=800, rating=7.4),),
            "series": (Look(genres="16", without_genres="10751|10762", votes=300, rating=7.6),),
        },
        alt_names=("Adult Animation", "Animation for Grown-Ups", "Cartoons After Bedtime"),
    ),
    Spec(
        "documentaries",
        "evergreen",
        "Docs Worth Your Time",
        {
            "movie": (Look(genres="99", votes=300, rating=7.3),),
            "series": (Look(genres="99", votes=150, rating=7.5),),
        },
        alt_names=("Real Stories, Great Docs", "Docs That Stay With You", "Documentary Night"),
    ),
    Spec(
        "sci_fi",
        "evergreen",
        "Sci-Fi to Stay Up For",
        {
            "movie": (Look(genres="878", votes=1500, rating=7.2),),
            "series": (Look(genres="10765", votes=600, rating=7.4),),
        },
        alt_names=("Sci-Fi Night", "Futures and Other Worlds", "Science Fiction Worth Staying Up For"),
    ),
    Spec(
        "crime",
        "evergreen",
        "Crime and Consequence",
        {
            "movie": (Look(genres="80", votes=1000, rating=7.2),),
            "series": (Look(genres="80", votes=500, rating=7.5),),
        },
        alt_names=("Crime Night", "Wrong Side of the Law", "Crime Stories Done Well"),
    ),
    Spec(
        "fresh",
        "evergreen",
        "New and Worth It",
        {
            "movie": (Look(sort="popularity.desc", recent_days=150, votes=200, rating=7.0),),
            "series": (Look(sort="popularity.desc", recent_days=150, votes=100, rating=7.3),),
        },
        alt_names=("Fresh and Good", "New Releases Worth Your Time", "Just In and Worth It"),
    ),
    Spec(
        "made_in_nigeria",
        "evergreen",
        "Made in Nigeria",
        {
            "movie": (Look(countries="NG", sort="popularity.desc", votes=10, pages=4, language=None),),
            "series": (Look(countries="NG", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Naija Picks", "Nollywood and More", "Home-Grown Stories"),
    ),
    Spec(
        "cozy_night",
        "evergreen",
        "Cosy Night In",
        {
            "movie": (Look(genres="35|10749|10751", runtime=(None, 115), votes=800, rating=7.0),),
            "series": (Look(genres="35|10751", votes=300, rating=7.3),),
        },
        alt_names=("Blanket and Snacks", "Nothing Heavy Tonight"),
    ),
    Spec(
        "adrenaline",
        "evergreen",
        "Adrenaline Rush",
        {
            "movie": (Look(genres="28|53", votes=1500, rating=7.0),),
            "series": (Look(genres="10759|80", votes=500, rating=7.4),),
        },
        alt_names=("Heart-Racing Picks", "Hold Onto Your Seat"),
    ),
    Spec(
        "slow_burns",
        "evergreen",
        "Slow Burns",
        {
            "movie": (Look(genres="18|53|9648", runtime=(110, None), votes=1000, rating=7.4),),
            "series": (Look(genres="18|9648", votes=400, rating=7.6),),
        },
        alt_names=("Worth the Patience", "Let It Simmer"),
    ),
    Spec(
        "date_night",
        "evergreen",
        "Date Night",
        {
            "movie": (Look(genres="10749|35", runtime=(90, 125), votes=1000, rating=7.0),),
            "series": (Look(genres="18|35", keywords=("romance", "relationship"), votes=300, rating=7.3),),
        },
        alt_names=("Dinner and a Movie", "Something for Two"),
    ),
    Spec(
        "family_night",
        "evergreen",
        "Family Movie Night",
        {
            "movie": (Look(genres="10751|16|12", votes=1500, rating=7.0),),
            "series": (Look(genres="10751|16", votes=400, rating=7.4),),
        },
        alt_names=("Everyone on the Couch", "Fun for All Ages"),
    ),
    Spec(
        "big_laughs",
        "evergreen",
        "Big Laughs",
        {
            "movie": (Look(genres="35", votes=1500, rating=7.0),),
            "series": (Look(genres="35", votes=500, rating=7.6),),
        },
        alt_names=("Proper Funny", "Cry-Laughing Material"),
    ),
    Spec(
        "tearjerkers",
        "evergreen",
        "Tearjerkers",
        {
            "movie": (
                Look(
                    genres="18|10749",
                    keywords=("grief", "terminal illness", "loss of loved one"),
                    votes=800,
                    rating=7.3,
                ),
            ),
            "series": (
                Look(genres="18", keywords=("grief", "terminal illness", "loss of loved one"), votes=200, rating=7.5),
            ),
        },
        alt_names=("Bring Tissues", "Heavy on the Feels"),
    ),
    Spec(
        "true_crime",
        "evergreen",
        "True Crime Files",
        {
            "movie": (Look(genres="99,80", votes=150, rating=7.0),),
            "series": (Look(genres="99,80", votes=100, rating=7.2),),
        },
        alt_names=("Real Cases, Real Fallout", "Crimes That Happened"),
    ),
    Spec(
        "great_pretenders",
        "evergreen",
        "Great Pretenders",
        {
            "movie": (Look(keywords=("con artist", "impostor", "fraud", "scam"), votes=400, rating=6.9),),
            "series": (Look(keywords=("con artist", "impostor", "fraud", "scam"), votes=150, rating=7.2),),
        },
        alt_names=("Fakers and Frauds", "Nothing but a Con"),
    ),
    Spec(
        "spy_games",
        "evergreen",
        "Spy Games",
        {
            "movie": (Look(keywords=("spy", "secret agent", "espionage", "double agent"), votes=500, rating=6.9),),
            "series": (Look(keywords=("spy", "secret agent", "espionage", "double agent"), votes=150, rating=7.2),),
        },
        alt_names=("Cloak and Dagger", "Secret Agents at Work"),
    ),
    Spec(
        "courtroom",
        "evergreen",
        "Courtroom Drama",
        {
            "movie": (Look(keywords=("courtroom", "lawyer", "trial"), votes=500, rating=7.0),),
            "series": (Look(keywords=("courtroom", "lawyer", "trial"), votes=150, rating=7.3),),
        },
        alt_names=("Order in the Court", "Your Honour"),
    ),
    Spec(
        "hospital",
        "evergreen",
        "Code Blue",
        {
            "movie": (Look(keywords=("hospital", "doctor", "surgeon", "nurse"), votes=400, rating=7.0),),
            "series": (Look(keywords=("hospital", "doctor", "surgeon", "nurse"), votes=200, rating=7.3),),
        },
        alt_names=("Doctors and Drama", "Life in the Ward"),
    ),
    Spec(
        "power_games",
        "evergreen",
        "Power Corrupts",
        {
            "movie": (
                Look(keywords=("politics", "political corruption", "president", "election"), votes=500, rating=7.0),
            ),
            "series": (
                Look(keywords=("politics", "political corruption", "president", "election"), votes=200, rating=7.4),
            ),
        },
        alt_names=("Corridors of Power", "Politics Behind Closed Doors"),
    ),
    Spec(
        "newsroom",
        "evergreen",
        "Stop the Presses",
        {
            "movie": (Look(keywords=("journalist", "journalism", "newspaper", "reporter"), votes=300, rating=7.0),),
            "series": (Look(keywords=("journalist", "journalism", "newspaper", "reporter"), votes=100, rating=7.3),),
        },
        alt_names=("Chasing the Story", "Newsroom Drama"),
    ),
    Spec(
        "war_stories",
        "evergreen",
        "Under Fire",
        {
            "movie": (Look(genres="10752", votes=1000, rating=7.3),),
            "series": (Look(genres="10768", votes=300, rating=7.5),),
        },
        alt_names=("War Stories", "Courage on the Front Line"),
    ),
    Spec(
        "westerns",
        "evergreen",
        "Saddle Up",
        {
            "movie": (Look(genres="37", votes=500, rating=7.2),),
            "series": (Look(genres="37", votes=100, rating=7.3),),
        },
        alt_names=("Dust, Guns and Gold", "Riders and Outlaws"),
    ),
    Spec(
        "sing_along",
        "evergreen",
        "Sing Along",
        {
            "movie": (Look(genres="10402", keywords=("musical",), votes=500, rating=7.0),),
            "series": (Look(keywords=("musical", "music"), votes=100, rating=7.3),),
        },
        alt_names=("Big Musical Numbers", "Showstoppers"),
    ),
    Spec(
        "game_on",
        "evergreen",
        "Game On",
        {
            "movie": (Look(keywords=("sports", "boxing", "coach", "underdog"), votes=500, rating=7.0),),
            "series": (Look(keywords=("sports", "boxing", "coach", "underdog"), votes=100, rating=7.3),),
        },
        alt_names=("Sports Dramas", "Underdogs and Champions"),
    ),
    Spec(
        "beyond_earth",
        "evergreen",
        "Beyond Earth",
        {
            "movie": (Look(keywords=("space", "astronaut", "space travel", "spaceship"), votes=800, rating=7.0),),
            "series": (Look(keywords=("space", "astronaut", "space travel", "spaceship"), votes=200, rating=7.3),),
        },
        alt_names=("To the Stars", "Out in Space"),
    ),
    Spec(
        "end_times",
        "evergreen",
        "The End of the World",
        {
            "movie": (
                Look(
                    keywords=("post-apocalyptic future", "apocalypse", "end of the world", "dystopia"),
                    votes=600,
                    rating=6.9,
                ),
            ),
            "series": (
                Look(
                    keywords=("post-apocalyptic future", "apocalypse", "end of the world", "dystopia"),
                    votes=200,
                    rating=7.3,
                ),
            ),
        },
        alt_names=("Last Ones Standing", "After the Collapse"),
    ),
    Spec(
        "time_twists",
        "evergreen",
        "Time Twisters",
        {
            "movie": (Look(keywords=("time travel", "time loop", "time machine"), votes=500, rating=7.0),),
            "series": (Look(keywords=("time travel", "time loop", "time machine"), votes=150, rating=7.3),),
        },
        alt_names=("Rewind and Fast-Forward", "Timelines Gone Wrong"),
    ),
    Spec(
        "heroes",
        "evergreen",
        "Heroes and Villains",
        {
            "movie": (Look(keywords=("superhero", "supervillain", "superpower"), votes=1500, rating=6.8),),
            "series": (Look(keywords=("superhero", "supervillain", "superpower"), votes=400, rating=7.2),),
        },
        alt_names=("Capes, Masks and Mayhem", "Super Powered"),
    ),
    Spec(
        "quests",
        "evergreen",
        "Quests and Kingdoms",
        {
            "movie": (
                Look(keywords=("dragon", "wizard", "magic", "sword and sorcery", "quest"), votes=800, rating=6.9),
            ),
            "series": (
                Look(keywords=("dragon", "wizard", "magic", "sword and sorcery", "quest"), votes=300, rating=7.3),
            ),
        },
        alt_names=("Dragons and Wizards", "Fantasy Worth the Journey"),
    ),
    Spec(
        "creature_features",
        "evergreen",
        "Creature Features",
        {
            "movie": (Look(keywords=("monster", "giant monster", "creature", "kaiju"), votes=400, rating=6.3),),
            "series": (Look(keywords=("monster", "giant monster", "creature", "kaiju"), votes=100, rating=7.0),),
        },
        floor=6.2,
        alt_names=("Things With Teeth", "Monster Mash"),
    ),
    Spec(
        "bump_in_the_night",
        "evergreen",
        "Things That Go Bump",
        {
            "movie": (Look(keywords=("ghost", "haunted house", "possession", "exorcism"), votes=500, rating=6.4),),
            "series": (Look(keywords=("ghost", "haunted house", "possession", "exorcism"), votes=150, rating=7.0),),
        },
        floor=6.3,
        alt_names=("Haunted and Hunted", "Don't Turn Off the Lights"),
    ),
    Spec(
        "slashers",
        "evergreen",
        "Slashers and Final Girls",
        {
            "movie": (Look(keywords=("slasher", "serial killer"), votes=400, rating=6.2),),
        },
        types=("movie",),
        floor=6.1,
        alt_names=("Masked Killers", "Stalk and Slash"),
    ),
    Spec(
        "head_games",
        "evergreen",
        "Messing With Your Head",
        {
            "movie": (
                Look(
                    keywords=("psychological thriller", "paranoia", "unreliable narrator", "gaslighting"),
                    votes=500,
                    rating=6.8,
                ),
            ),
            "series": (
                Look(
                    keywords=("psychological thriller", "paranoia", "unreliable narrator", "gaslighting"),
                    votes=150,
                    rating=7.2,
                ),
            ),
        },
        alt_names=("Mind Games", "Is It Real?"),
    ),
    Spec(
        "survival",
        "evergreen",
        "Survival Stories",
        {
            "movie": (Look(keywords=("survival", "stranded", "shipwreck", "wilderness"), votes=500, rating=6.8),),
            "series": (Look(keywords=("survival", "stranded", "shipwreck", "wilderness"), votes=150, rating=7.2),),
        },
        alt_names=("Fight to Survive", "Stranded"),
    ),
    Spec(
        "disaster_zone",
        "evergreen",
        "Disaster Zone",
        {
            "movie": (
                Look(
                    keywords=("natural disaster", "earthquake", "volcano", "tsunami", "flood", "asteroid"),
                    votes=300,
                    rating=6.5,
                ),
            ),
            "series": (
                Look(
                    keywords=("natural disaster", "earthquake", "volcano", "tsunami", "flood", "asteroid"),
                    votes=100,
                    rating=7.0,
                ),
            ),
        },
        alt_names=("When It All Goes Wrong", "Nature Strikes Back"),
    ),
    Spec(
        "road_trips",
        "evergreen",
        "On the Road",
        {
            "movie": (Look(keywords=("road trip", "road movie"), votes=500, rating=6.9),),
            "series": (Look(keywords=("road trip", "road movie"), votes=100, rating=7.2),),
        },
        alt_names=("Hit the Road", "Road Trip Rides"),
    ),
    Spec(
        "growing_up",
        "evergreen",
        "Growing Up",
        {
            "movie": (Look(keywords=("coming of age", "first love"), votes=600, rating=7.0),),
            "series": (Look(keywords=("coming of age", "first love"), votes=200, rating=7.3),),
        },
        alt_names=("Growing Pains", "The Awkward Years"),
    ),
    Spec(
        "rom_coms",
        "evergreen",
        "Rom-Com Comfort",
        {
            "movie": (Look(genres="35,10749", votes=1200, rating=6.8),),
            "series": (Look(genres="35", keywords=("romance", "love"), votes=300, rating=7.3),),
        },
        alt_names=("Meet-Cutes and Mix-Ups", "Laughing and Falling in Love"),
    ),
    Spec(
        "love_stories",
        "evergreen",
        "Love Stories That Last",
        {
            "movie": (Look(genres="10749,18", votes=1000, rating=7.3),),
            "series": (Look(genres="18", keywords=("romance", "love"), votes=300, rating=7.5),),
        },
        alt_names=("Swoon-Worthy Romance", "For the Hopeless Romantics"),
    ),
    Spec(
        "best_friends",
        "evergreen",
        "Best Friends Forever",
        {
            "movie": (Look(keywords=("buddy comedy", "best friend", "buddy cop", "bromance"), votes=600, rating=6.8),),
            "series": (Look(keywords=("buddy comedy", "best friend", "buddy cop", "bromance"), votes=150, rating=7.2),),
        },
        alt_names=("Partners in Crime", "Ride or Die"),
    ),
    Spec(
        "family_secrets",
        "evergreen",
        "Family Secrets",
        {
            "movie": (
                Look(
                    keywords=("family secrets", "dysfunctional family", "inheritance", "family reunion"),
                    votes=300,
                    rating=7.0,
                ),
            ),
            "series": (
                Look(
                    keywords=("family secrets", "dysfunctional family", "inheritance", "family reunion"),
                    votes=200,
                    rating=7.3,
                ),
            ),
        },
        alt_names=("Skeletons in the Closet", "Family Drama"),
    ),
    Spec(
        "small_town_mysteries",
        "evergreen",
        "Welcome to Nowhere",
        {
            "movie": (Look(genres="9648|18|53", keywords=("small town",), votes=400, rating=6.8),),
            "series": (Look(genres="9648|18|80", keywords=("small town",), votes=150, rating=7.3),),
        },
        alt_names=("Small Town, Big Secrets", "Everybody Knows Everybody"),
    ),
    Spec(
        "island_getaways",
        "evergreen",
        "Island Getaways",
        {
            "movie": (Look(keywords=("island", "tropical island", "beach"), votes=400, rating=6.5),),
            "series": (Look(keywords=("island", "tropical island", "beach"), votes=100, rating=7.0),),
        },
        alt_names=("Paradise Found", "Stranded in Paradise"),
    ),
    Spec(
        "city_lights",
        "evergreen",
        "City Lights",
        {
            "movie": (
                Look(
                    genres="18|10749|35",
                    keywords=("new york city", "paris, france", "london, england"),
                    votes=800,
                    rating=7.0,
                ),
            ),
            "series": (
                Look(
                    genres="18|10749|35",
                    keywords=("new york city", "paris, france", "london, england"),
                    votes=300,
                    rating=7.3,
                ),
            ),
        },
        alt_names=("Big City Stories", "Streets Worth Walking"),
    ),
    Spec(
        "food_on_screen",
        "evergreen",
        "Food on Screen",
        {
            "movie": (Look(keywords=("chef", "restaurant", "cooking", "food"), votes=200, rating=6.8),),
            "series": (Look(keywords=("chef", "restaurant", "cooking", "food"), votes=100, rating=7.3),),
        },
        alt_names=("Hungry Yet?", "Chefs and Kitchens"),
    ),
    Spec(
        "behind_the_music",
        "evergreen",
        "Behind the Music",
        {
            "movie": (Look(keywords=("rapper", "hip-hop", "musician", "singer", "band"), votes=200, rating=6.8),),
            "series": (Look(keywords=("rapper", "hip-hop", "musician", "singer", "band"), votes=100, rating=7.2),),
        },
        alt_names=("Beats and Rhymes", "Life on Stage"),
    ),
    Spec(
        "showbiz",
        "evergreen",
        "Lights, Camera, Drama",
        {
            "movie": (Look(keywords=("hollywood", "film industry", "actor", "theater"), votes=300, rating=7.0),),
            "series": (Look(keywords=("hollywood", "film industry", "actor", "theater"), votes=100, rating=7.3),),
        },
        alt_names=("Behind the Silver Screen", "Hollywood on Hollywood"),
    ),
    Spec(
        "laugh_if_you_dare",
        "evergreen",
        "Laugh If You Dare",
        {
            "movie": (Look(keywords=("dark comedy", "black comedy", "satire", "mockumentary"), votes=500, rating=7.0),),
            "series": (
                Look(keywords=("dark comedy", "black comedy", "satire", "mockumentary"), votes=150, rating=7.4),
            ),
        },
        alt_names=("Dark Humour", "Funny but Twisted"),
    ),
    Spec(
        "mockumentaries",
        "evergreen",
        "Fake Docs, Real Laughs",
        {
            "movie": (Look(keywords=("mockumentary",), votes=100, rating=7.0),),
            "series": (Look(keywords=("mockumentary",), votes=100, rating=7.3),),
        },
        alt_names=("Mockumentaries", "Deadpan Docs"),
    ),
    Spec(
        "stand_up",
        "evergreen",
        "Stand-Up Specials",
        {
            "movie": (Look(keywords=("stand-up comedy",), votes=100, rating=6.8),),
        },
        types=("movie",),
        alt_names=("Comics on Stage", "Mic Drop"),
    ),
    Spec(
        "biopics",
        "evergreen",
        "Real Lives",
        {
            "movie": (Look(genres="18", keywords=("biography",), votes=800, rating=7.2),),
            "series": (Look(keywords=("biography",), votes=100, rating=7.4),),
        },
        alt_names=("Biopics Worth Watching", "Life Stories"),
    ),
    Spec(
        "history_alive",
        "evergreen",
        "History Comes Alive",
        {
            "movie": (Look(genres="36", votes=800, rating=7.2),),
            "series": (Look(keywords=("period drama", "history"), votes=150, rating=7.4),),
        },
        alt_names=("Stories From the Past", "Look Back in Time"),
    ),
    Spec(
        "ancient_worlds",
        "evergreen",
        "Ancient Worlds",
        {
            "movie": (Look(keywords=("ancient rome", "ancient greece", "gladiator", "egypt"), votes=500, rating=6.8),),
            "series": (Look(keywords=("ancient rome", "ancient greece", "gladiator", "egypt"), votes=150, rating=7.2),),
        },
        alt_names=("Swords and Sandals", "Rome, Greece and Beyond"),
    ),
    Spec(
        "knights_and_kings",
        "evergreen",
        "Knights and Kings",
        {
            "movie": (Look(keywords=("knight", "medieval", "viking", "king"), votes=400, rating=6.6),),
            "series": (Look(keywords=("knight", "medieval", "viking", "king"), votes=150, rating=7.2),),
        },
        alt_names=("Crowns and Castles", "Age of Chivalry"),
    ),
    Spec(
        "high_seas",
        "evergreen",
        "High Seas",
        {
            "movie": (Look(keywords=("pirate", "shipwreck", "sailor"), votes=400, rating=6.5),),
            "series": (Look(keywords=("pirate", "shipwreck", "sailor"), votes=100, rating=7.0),),
        },
        alt_names=("Pirates and Plunder", "Sailing Into Trouble"),
    ),
    Spec(
        "noir_nights",
        "evergreen",
        "Neo-Noir Nights",
        {
            "movie": (
                Look(keywords=("film noir", "neo-noir", "femme fatale", "private detective"), votes=300, rating=7.0),
            ),
            "series": (
                Look(keywords=("film noir", "neo-noir", "femme fatale", "private detective"), votes=100, rating=7.2),
            ),
        },
        alt_names=("Shadows and Smoke", "Dark Streets, Darker Secrets"),
    ),
    Spec(
        "cult_favourites",
        "evergreen",
        "Cult Favourites",
        {
            "movie": (Look(keywords=("cult film",), votes=500, rating=6.8),),
        },
        types=("movie",),
        alt_names=("Cult Classics", "Midnight Movie Picks"),
    ),
    Spec(
        "recent_greats",
        "evergreen",
        "Recent Greats",
        {
            "movie": (Look(recent_days=SIX_YEARS, votes=2500, rating=7.6),),
            "series": (Look(recent_days=SIX_YEARS, votes=1000, rating=8.0),),
        },
        alt_names=("Modern Must-Watches", "The Best of the Last Few Years"),
    ),
    Spec(
        "sitcoms",
        "evergreen",
        "Sitcom Comfort",
        {
            "series": (Look(keywords=("sitcom",), votes=200, rating=7.3),),
        },
        types=("series",),
        alt_names=("Laugh-Track Favourites", "Easy Watching"),
    ),
    Spec(
        "animated_adventures",
        "evergreen",
        "Animation for Everyone",
        {
            "movie": (Look(genres="16,10751", votes=1500, rating=7.2),),
            "series": (Look(genres="16,10751", votes=400, rating=7.4),),
        },
        alt_names=("Animated Adventures", "Drawn Together"),
    ),
    Spec(
        "wild_planet",
        "evergreen",
        "Wild Planet",
        {
            "movie": (Look(genres="99", keywords=("nature", "wildlife", "ocean"), votes=150, rating=7.3),),
            "series": (Look(genres="99", keywords=("nature", "wildlife", "ocean"), votes=100, rating=7.6),),
        },
        alt_names=("Nature and Wild Places", "The Natural World"),
    ),
    Spec(
        "curious_minds",
        "evergreen",
        "Docs for Curious Minds",
        {
            "movie": (Look(genres="99", keywords=("science", "technology", "universe"), votes=150, rating=7.2),),
            "series": (Look(genres="99", keywords=("science", "technology", "universe"), votes=100, rating=7.5),),
        },
        alt_names=("Big Ideas", "Science and Society"),
    ),
    Spec(
        "music_docs",
        "evergreen",
        "Music Docs",
        {
            "movie": (Look(genres="99,10402", votes=100, rating=7.2),),
        },
        types=("movie",),
        alt_names=("The Story Behind the Songs", "Backstage Passes"),
    ),
    Spec(
        "sports_docs",
        "evergreen",
        "Sports Docs",
        {
            "movie": (
                Look(
                    genres="99", keywords=("sports", "football (soccer)", "basketball", "boxing"), votes=100, rating=7.2
                ),
            ),
            "series": (
                Look(
                    genres="99", keywords=("sports", "football (soccer)", "basketball", "boxing"), votes=100, rating=7.4
                ),
            ),
        },
        alt_names=("Behind the Game", "The Real Story of the Sport"),
    ),
    Spec(
        "whodunits",
        "evergreen",
        "Whodunits",
        {
            "movie": (Look(genres="9648", votes=800, rating=7.0),),
            "series": (Look(genres="9648", votes=300, rating=7.4),),
        },
        alt_names=("Who Did It?", "Murder, Mystery and Misdirection"),
    ),
    Spec(
        "quick_laughs",
        "evergreen",
        "Quick Laughs",
        {
            "series": (Look(genres="35", runtime=(None, 25), votes=300, rating=7.3),),
        },
        types=("series",),
        alt_names=("Short Episodes, Big Laughs", "Under 25 Minutes"),
    ),
    Spec(
        "weird_and_wonderful",
        "evergreen",
        "Weird and Wonderful",
        {
            "movie": (Look(keywords=("surrealism", "absurdism"), votes=300, rating=7.0),),
            "series": (Look(keywords=("surrealism", "absurdism"), votes=100, rating=7.3),),
        },
        alt_names=("Strange but Good", "Off the Beaten Path"),
    ),
    Spec(
        "pets_and_pals",
        "evergreen",
        "Pets and Pals",
        {
            "movie": (Look(keywords=("dog", "cat", "horse", "pet"), votes=800, rating=6.8),),
            "series": (Look(keywords=("dog", "cat", "horse", "pet"), votes=100, rating=7.2),),
        },
        alt_names=("Animal Companions", "Good Dogs and Good Cats"),
    ),
    Spec(
        "teen_dramas",
        "evergreen",
        "Teen Dramas",
        {
            "movie": (Look(genres="18", keywords=("teenager", "high school"), votes=600, rating=7.0),),
            "series": (Look(genres="18", keywords=("teenager", "high school"), votes=300, rating=7.3),),
        },
        alt_names=("Growing Up Messy", "High School, Higher Stakes"),
    ),
    Spec(
        "cyberpunk",
        "evergreen",
        "Neon and Chrome",
        {
            "movie": (
                Look(
                    keywords=("cyberpunk", "android", "virtual reality", "artificial intelligence (a.i.)"),
                    votes=500,
                    rating=6.8,
                ),
            ),
            "series": (
                Look(
                    keywords=("cyberpunk", "android", "virtual reality", "artificial intelligence (a.i.)"),
                    votes=150,
                    rating=7.3,
                ),
            ),
        },
        alt_names=("Cyberpunk Futures", "High Tech, Low Life"),
    ),
    Spec(
        "nollywood_thrillers",
        "evergreen",
        "Nollywood Thrillers",
        {
            "movie": (
                Look(genres="53|80|9648", countries="NG", sort="popularity.desc", votes=10, pages=3, language=None),
            ),
            "series": (
                Look(genres="80|9648|18", countries="NG", sort="popularity.desc", votes=5, pages=2, language=None),
            ),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Naija Suspense", "Nigerian Crime and Mystery"),
    ),
    Spec(
        "nollywood_comedy",
        "evergreen",
        "Nollywood Comedy",
        {
            "movie": (Look(genres="35", countries="NG", sort="popularity.desc", votes=10, pages=3, language=None),),
            "series": (Look(genres="35", countries="NG", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Naija Laughs", "Funny From Nigeria"),
    ),
    Spec(
        "nollywood_love",
        "evergreen",
        "Nollywood Love Stories",
        {
            "movie": (
                Look(genres="10749|18", countries="NG", sort="popularity.desc", votes=10, pages=3, language=None),
            ),
            "series": (Look(genres="18", countries="NG", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Naija Romance", "Love, Lagos Style"),
    ),
    Spec(
        "nollywood_action",
        "evergreen",
        "Action and Epics From Nigeria",
        {
            "movie": (
                Look(genres="28|12|14|878", countries="NG", sort="popularity.desc", votes=10, pages=3, language=None),
            ),
            "series": (
                Look(genres="10759|10765", countries="NG", sort="popularity.desc", votes=5, pages=2, language=None),
            ),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Naija Action and Adventure", "Big Nigerian Productions"),
    ),
    Spec(
        "ghana_stories",
        "evergreen",
        "Ghana Stories",
        {
            "movie": (Look(countries="GH", sort="popularity.desc", votes=10, pages=3, language=None),),
            "series": (Look(countries="GH", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Made in Ghana", "Ghollywood Picks"),
    ),
    Spec(
        "south_africa_stories",
        "evergreen",
        "South African Stories",
        {
            "movie": (Look(countries="ZA", sort="popularity.desc", votes=10, pages=3, language=None),),
            "series": (Look(countries="ZA", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Mzansi on Screen", "Made in South Africa"),
    ),
    Spec(
        "east_africa_stories",
        "evergreen",
        "East African Stories",
        {
            "movie": (Look(countries="KE|TZ|UG|ET", sort="popularity.desc", votes=10, pages=3, language=None),),
            "series": (Look(countries="KE|TZ|UG|ET", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Made in East Africa", "Nairobi, Dar and Addis"),
    ),
    Spec(
        "north_africa_cinema",
        "evergreen",
        "North African Cinema",
        {
            "movie": (Look(countries="EG|MA|TN|DZ", sort="popularity.desc", votes=10, pages=3, language=None),),
            "series": (Look(countries="EG|MA|TN|DZ", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Cairo to Casablanca", "Made in North Africa"),
    ),
    Spec(
        "francophone_africa",
        "evergreen",
        "Francophone Africa",
        {
            "movie": (Look(countries="SN|CI|CM", sort="popularity.desc", votes=10, pages=3, language=None),),
            "series": (Look(countries="SN|CI|CM", sort="popularity.desc", votes=5, pages=2, language=None),),
        },
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Dakar to Douala", "Stories From Francophone Africa"),
    ),
    Spec(
        "african_series",
        "evergreen",
        "African Series",
        {
            "series": (Look(countries=AFRICA, sort="popularity.desc", votes=5, pages=3, language=None),),
        },
        types=("series",),
        floor=6.0,
        min_votes=5,
        ignores_popularity=True,
        alt_names=("Binge-Worthy African TV", "Made for TV in Africa", "Series From the Continent"),
    ),
    Spec(
        "independence_day",
        "season",
        "Freedom and Nationhood",
        {
            "movie": (Look(keywords=("revolution", "apartheid", "resistance", "civil rights"), votes=300, rating=7.0),),
            "series": (
                Look(keywords=("revolution", "apartheid", "resistance", "civil rights"), votes=100, rating=7.2),
            ),
        },
        window=((9, 28), (10, 2)),
        min_votes=100,
        alt_names=("Stories of Independence", "Fighting for Freedom"),
    ),
    Spec(
        "democracy_day",
        "season",
        "Power and the People",
        {
            "movie": (
                Look(keywords=("election", "politics", "democracy", "political corruption"), votes=300, rating=7.0),
            ),
            "series": (
                Look(keywords=("election", "politics", "democracy", "political corruption"), votes=100, rating=7.2),
            ),
        },
        window=((6, 10), (6, 13)),
        alt_names=("Democracy on Screen", "Votes and Voices"),
    ),
    Spec(
        "childrens_day",
        "season",
        "Children's Day Picks",
        {
            "movie": (Look(genres="16|10751", votes=1000, rating=7.0),),
            "series": (Look(genres="16|10751", votes=200, rating=7.2),),
        },
        window=((5, 25), (5, 28)),
        alt_names=("Fun for the Little Ones", "Kids' Corner"),
    ),
    Spec(
        "mothers_day",
        "season",
        "For Mum",
        {
            "movie": (
                Look(
                    keywords=("mother daughter relationship", "mother son relationship", "single mother"),
                    votes=300,
                    rating=7.0,
                ),
            ),
            "series": (
                Look(
                    keywords=("mother daughter relationship", "mother son relationship", "single mother"),
                    votes=100,
                    rating=7.2,
                ),
            ),
        },
        window=((5, 5), (5, 12)),
        alt_names=("Mums on Screen", "Mother Knows Best"),
    ),
    Spec(
        "fathers_day",
        "season",
        "For Dad",
        {
            "movie": (
                Look(
                    keywords=("father son relationship", "father daughter relationship", "single father"),
                    votes=300,
                    rating=7.0,
                ),
            ),
            "series": (
                Look(
                    keywords=("father son relationship", "father daughter relationship", "single father"),
                    votes=100,
                    rating=7.2,
                ),
            ),
        },
        window=((6, 14), (6, 21)),
        alt_names=("Dads on Screen", "Father Figures"),
    ),
    Spec(
        "womens_day",
        "season",
        "Women Leading the Story",
        {
            "movie": (Look(keywords=("female protagonist", "feminism", "woman director"), votes=500, rating=7.0),),
            "series": (Look(keywords=("female protagonist", "feminism", "woman director"), votes=200, rating=7.3),),
        },
        window=((3, 1), (3, 9)),
        alt_names=("Her Turn", "Women Who Run the Show"),
    ),
    Spec(
        "easter",
        "season",
        "Easter Weekend",
        {
            "movie": (
                Look(
                    genres="10751|18|12",
                    keywords=("easter", "faith", "religion", "jesus christ"),
                    votes=200,
                    rating=6.8,
                ),
                Look(genres="10751|35|10749", votes=1500, rating=7.0),
            ),
            "series": (Look(keywords=("easter", "faith", "religion", "jesus christ"), votes=100, rating=7.0),),
        },
        window=((3, 25), (4, 22)),
        alt_names=("Hope and Renewal", "Long Weekend Watching"),
    ),
    Spec(
        "thanksgiving",
        "season",
        "Gather Round",
        {
            "movie": (
                Look(
                    genres="35|10751|18", keywords=("thanksgiving", "family reunion", "dinner"), votes=200, rating=6.8
                ),
                Look(genres="35|10751", votes=1500, rating=7.0),
            ),
            "series": (Look(genres="35|10751", votes=300, rating=7.3),),
        },
        window=((11, 15), (11, 28)),
        alt_names=("Family Feasts", "Thanksgiving Tables"),
    ),
    Spec(
        "black_voices",
        "season",
        "Black Voices on Screen",
        {
            "movie": (Look(keywords=("african american", "civil rights", "racism"), votes=300, rating=7.0),),
            "series": (Look(keywords=("african american", "civil rights", "racism"), votes=100, rating=7.2),),
        },
        window=((2, 1), (2, 28)),
        alt_names=("Black Stories, Told Well", "Celebrating Black Cinema"),
    ),
    Spec(
        "harmattan",
        "season",
        "Harmattan Nights",
        {
            "movie": (
                Look(genres="9648|18|10749", keywords=("winter", "snow", "cold"), votes=300, rating=7.0),
                Look(genres="18|10749", votes=1500, rating=7.4),
            ),
            "series": (Look(genres="18|9648", votes=500, rating=7.5),),
        },
        window=((1, 5), (2, 25)),
        alt_names=("Cold Night, Warm Blanket", "Wrapped Up and Watching"),
    ),
    Spec(
        "love_is_overrated",
        "season",
        "Love Is Overrated",
        {
            "movie": (Look(genres="35|18", keywords=("breakup", "divorce", "heartbreak"), votes=300, rating=6.9),),
            "series": (Look(genres="35|18", keywords=("breakup", "divorce", "heartbreak"), votes=150, rating=7.2),),
        },
        window=((2, 5), (2, 14)),
        alt_names=("Anti-Valentine's Picks", "Single and Thriving"),
    ),
    Spec(
        "year_in_review",
        "season",
        "Best of the Year",
        {
            "movie": (Look(sort="vote_average.desc", recent_days=365, votes=1500, rating=7.4),),
            "series": (Look(sort="vote_average.desc", recent_days=365, votes=500, rating=7.8),),
        },
        window=((12, 15), (12, 31)),
        alt_names=("The Year's Best", "Don't Miss These From This Year"),
    ),
    Spec(
        "tuesday",
        "moment",
        "Tuesday Twist",
        {
            "movie": (Look(genres="9648|53|35", votes=1000, rating=7.0),),
            "series": (Look(genres="9648|35|80", votes=400, rating=7.3),),
        },
        alt_names=("Tuesday Night Pick", "Midweek Warm-Up"),
    ),
    Spec(
        "month_start",
        "moment",
        "New Month, New Watchlist",
        {
            "movie": (Look(sort="popularity.desc", recent_days=365, votes=500, rating=7.2),),
            "series": (Look(sort="popularity.desc", recent_days=365, votes=200, rating=7.4),),
        },
        alt_names=("Fresh Month, Fresh Picks", "Start the Month Right"),
    ),
)
SPEC_BY_KEY = {s.key: s for s in SPECS}
# Rows that belong to one weekday (Monday is 0).
WEEKDAY_ROWS = {"monday": 0, "tuesday": 1, "midweek": 2, "thursday": 3}


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
    if spec.key in WEEKDAY_ROWS:
        return today_.day.weekday() == WEEKDAY_ROWS[spec.key]
    if spec.key == "payday":
        return today_.day.day >= 25
    if spec.key == "month_start":
        return today_.day.day <= 4
    if spec.key == "rainy":
        return today_.rain
    if spec.key == "people":
        return today_.has_people_seed
    return True


def choose_specs(types: list[str], today_: Today, shown: dict[str, str], rng: random.Random, count: int) -> list[Spec]:
    """`count` distinct specs for a mixed set of rows, in slot order. Pinned rows come
    first. The rest cycle through season, moment and evergreen kinds, taking the spec
    shown longest ago in each, so every row that fits the day gets its turn. A kind with
    nothing that fits (no season, a dry weekday, every one shown this week) hands its place
    to the evergreen rows."""
    recent = (today_.day - timedelta(days=REPEAT_AFTER_DAYS - 1)).isoformat()
    usable = [
        s
        for s in SPECS
        if set(s.types) & set(types) and is_eligible(s, today_) and (s.pinned or shown.get(s.key, "") < recent)
    ]
    chosen = [s for s in usable if s.pinned][:count]
    misses = 0
    kinds = itertools.cycle(SLOT_KINDS)
    while len(chosen) < count and misses < len(SLOT_KINDS):
        kind = next(kinds)
        free = [s for s in usable if s not in chosen]
        available = [s for s in free if s.kind == kind] or [s for s in free if s.kind == "evergreen"]
        if not available:
            misses += 1
            continue
        misses = 0
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
    """Today's seasonal and contextual rows: `config.rows` in all, as `watchly.ctx.1..n`
    slots under one type, each row blending movies and series where it can.

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
    key = f"{config.rows}:{'+'.join(types)}"
    raw = await redis_service.get(CONTEXT_LOG_KEY.format(token=token)) if token else None
    log: dict[str, Any] = json.loads(raw) if raw else {}
    if log.get("v") != LOG_VERSION or log.get("day") != day.isoformat():
        log = {"v": LOG_VERSION, "day": day.isoformat(), "shown": log.get("shown", {})}

    if log.get("key") != key:
        movie_pool = [i for i in library.loved + library.liked + library.watched if i.type == "movie"]
        today_ = Today(day, await rains_today(day), bool(movie_pool) and "movie" in types)
        rng = random.Random(f"{token}:{day.isoformat()}")
        picks = []
        for spec in choose_specs(types, today_, log["shown"], rng, config.rows):
            log["shown"][spec.key] = day.isoformat()
            picks.append(await _pick(token, spec, day, movie_pool, last_watched, year_min))
        log["picks"], log["key"] = picks, key
        if token:
            await redis_service.set(CONTEXT_LOG_KEY.format(token=token), json.dumps(log), LOG_TTL_SECONDS)

    home_type = types[0]
    extra = [] if config.display_at_home else DISCOVER_ONLY_EXTRA
    catalogs = []
    for slot, pick in enumerate(log["picks"], start=1):
        row_slots.setdefault(home_type, {})[f"ctx.{slot}"] = pick["definition"]
        catalogs.append({"type": home_type, "id": f"{CONTEXT_PREFIX}{slot}", "name": pick["name"], "extra": extra})
    return fill_slots(catalogs, CONTEXT_PREFIX, {home_type: config.rows}, config.name or "Seasonal & contextual", extra)


async def _pick(
    token: str | None,
    spec: Spec,
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
        decades = list(range(max(DEFAULT_YEAR_MIN, year_min // 10 * 10), day.year // 10 * 10 + 1, 10))
        arg = str(decades[day.toordinal() % len(decades)])
        fields["decade"] = arg
    name = random.Random(f"{spec.key}|{day.isoformat()}").choice((spec.name, *spec.alt_names))
    return {"definition": f"{spec.key}:{arg}" if arg else spec.key, "name": name.format(**fields)}


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
        level_found: dict[int, int] = {}
        usable: list[dict[str, Any]] = []
        slots = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)

        async def discover(params: dict[str, Any], page: int) -> dict[str, Any]:
            async with slots:
                return await self.tmdb_service.get_discover(mtype, page=page, **params)

        for level in range(LEVELS):
            plan = Plan(mtype, day, arg or None, level, profile)
            looks = await self._looks(spec, content_type, plan)
            keyword_ids = await asyncio.gather(*(self._keyword_ids(look.keywords) for look in looks))
            calls = []
            for look, ids in zip(looks, keyword_ids):
                if look.keywords and not ids:
                    continue
                params = look.params(mtype, level, spec.floor, day, ids)
                # Single-page looks (On This Day's windows) widen by date instead.
                pages = range(1, look.pages + (2 * level if look.pages > 1 else 0) + 1)
                calls.extend(discover(params, p) for p in pages)
            results = await asyncio.gather(*calls, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning(f"Discover for {key} failed: {type(result).__name__}")
                    continue
                for item in result.get("results", []):
                    if item.get("id"):
                        found.setdefault(item["id"], item)
                        level_found.setdefault(item["id"], level)
            unique = filter_items_by_settings(
                list(found.values()), self.user_settings, apply_quality_band=not spec.ignores_popularity
            )
            usable = [
                i
                for i in filter_by_genres(unique, watched_tmdb, excluded)
                if clears_floor(i, spec.floor, spec.min_votes)
                and (spec.max_popularity is None or (i.get("popularity") or 0) <= spec.max_popularity)
            ]
            if len(usable) >= row_size(limit):
                break

        usable.sort(key=lambda i: (level_found[i["id"]] if spec.nearest_first else 0, -self._score(i, profile, mtype)))
        enriched = await RecommendationMetadata.fetch_batch(
            self.tmdb_service, usable[: row_size(limit)], content_type, user_settings=self.user_settings
        )
        return filter_watched_by_imdb(enriched, watched_imdb)

    async def get_mixed(
        self, catalog_id: str, types: list[str], states: dict[str, TypeState], claimed: dict[str, set[int]], limit: int
    ) -> list[dict[str, Any]]:
        key = catalog_id.removeprefix(CONTEXT_PREFIX).partition(":")[0]
        row_types = [t for t in types if t in SPEC_BY_KEY[key].types]

        async def build(content_type: str, state: TypeState, share: int) -> list[dict[str, Any]]:
            return await self.get_recommendations(catalog_id, content_type, state[0], state[1], state[2], share)

        return await blend(row_types, states, claimed, limit, build)

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
