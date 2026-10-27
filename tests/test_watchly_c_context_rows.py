import asyncio
import random
from datetime import date, timedelta

import pytest

from app.core.settings import CatalogConfig, get_default_settings
from app.models.library import LibraryCollection, StremioLibraryItem
from app.watchly_c import context_rows
from app.watchly_c.context_rows import (
    CONTEXT_PREFIX,
    SPEC_BY_KEY,
    SPECS,
    ContextRowService,
    Today,
    choose_specs,
    context_catalogs,
)

TOKEN = "tok_ctx"


def at(day: str, rain: bool = False, people: bool = True) -> Today:
    return Today(date.fromisoformat(day), rain, people)


def keys(content_type: str, today_: Today, shown: dict[str, str] | None = None, count: int = 3) -> list[str]:
    return [s.key for s in choose_specs(content_type, today_, shown or {}, random.Random(1), count)]


def test_season_rows_only_appear_inside_their_window():
    assert "spooky" in [s.key for s in SPECS if context_rows.is_eligible(s, at("2026-10-27"))]
    assert "spooky" not in [s.key for s in SPECS if context_rows.is_eligible(s, at("2026-11-02"))]
    assert "holiday" not in [s.key for s in SPECS if context_rows.is_eligible(s, at("2026-10-27"))]
    assert "holiday" in [s.key for s in SPECS if context_rows.is_eligible(s, at("2026-12-25"))]
    assert "valentines" in [s.key for s in SPECS if context_rows.is_eligible(s, at("2026-02-14"))]


def test_moments_follow_the_weekday_and_the_rain():
    friday, sunday, monday = at("2026-10-23"), at("2026-10-25"), at("2026-10-26")
    eligible = lambda t: {s.key for s in SPECS if context_rows.is_eligible(s, t)}  # noqa: E731
    assert "weekend" in eligible(friday) and "sunday" not in eligible(friday)
    assert "sunday" in eligible(sunday)
    assert not eligible(monday) & {"weekend", "sunday", "rainy"}
    assert "rainy" in eligible(at("2026-10-26", rain=True))


def test_every_slot_is_filled_even_on_a_quiet_day():
    quiet = at("2026-04-14")

    for content_type in ("movie", "series"):
        assert len(keys(content_type, quiet)) == 3


def test_types_only_get_specs_that_suit_them():
    assert "people" not in keys("series", at("2026-04-14"), {"decade": "2026-04-13", "hidden_gems": "2026-04-13"})
    assert all(
        content_type in SPEC_BY_KEY[k].types
        for content_type in ("movie", "series")
        for k in keys(content_type, at("2026-10-27"))
    )


def test_the_rotating_slot_cycles_through_every_evergreen_row():
    seen: set[str] = set()
    shown: dict[str, str] = {}
    day = date(2026, 4, 14)
    for n in range(10):
        today_ = Today(day + timedelta(days=n), False, True)
        for spec in choose_specs("movie", today_, shown, random.Random(n), 3):
            shown[spec.key] = today_.day.isoformat()
            seen.add(spec.key)
    assert seen >= {s.key for s in SPECS if s.kind == "evergreen" and "movie" in s.types}


def test_no_people_row_without_a_movie_to_build_it_from():
    assert "people" not in keys("movie", at("2026-04-14", people=False), {})


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value
        return True


def movie(item_id: str) -> StremioLibraryItem:
    return StremioLibraryItem(_id=item_id, type="movie", name=f"Title {item_id}", temp=False, removed=False)


@pytest.fixture
def world(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(context_rows, "redis_service", fake)

    async def dry(day):
        return False

    monkeypatch.setattr(context_rows, "rains_today", dry)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))

    async def seeds(token, pool, count, bucket, last_watched):
        return pool[:count]

    monkeypatch.setattr(context_rows, "choose_seeds", seeds)
    return fake


def build(config=None, library=None):
    slots: dict[str, dict[str, str]] = {}
    rows = asyncio.run(
        context_catalogs(
            TOKEN,
            config or CatalogConfig(id="watchly.ctx", name="Seasonal", rows=3),
            library or LibraryCollection(watched=[movie("tt1")], source="nuvio"),
            lambda i: None,
            1990,
            slots,
        )
    )
    return rows, slots


def test_each_type_gets_three_numbered_slots_with_a_definition_each(world):
    rows, slots = build()

    assert sorted(r["id"] for r in rows if r["type"] == "movie") == [f"{CONTEXT_PREFIX}{n}" for n in (1, 2, 3)]
    assert set(slots) == {"movie", "series"}
    assert set(slots["movie"]) == {"ctx.1", "ctx.2", "ctx.3"}
    assert slots["movie"]["ctx.1"] == "on_this_day"
    assert slots["series"]["ctx.1"] in {"spooky", "spooky_gentle", "african"}


def test_a_days_picks_are_stable_across_builds(world):
    first, _ = build()
    second, _ = build()

    assert first == second


def test_a_disabled_catalog_or_type_gives_no_rows(world):
    assert build(CatalogConfig(id="watchly.ctx", enabled=False))[0] == []
    rows, _ = build(CatalogConfig(id="watchly.ctx", enabled_series=False, rows=3))
    assert {r["type"] for r in rows} == {"movie"}


def test_a_new_day_moves_on_to_rows_not_shown_yet(world, monkeypatch):
    _, first = build()
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 28))
    _, second = build()

    assert first["movie"]["ctx.3"] != second["movie"]["ctx.3"]


def result(tmdb_id: int, avg: float = 7.6, votes: int = 900) -> dict:
    return {
        "id": tmdb_id,
        "genre_ids": [27],
        "vote_average": avg,
        "vote_count": votes,
        "original_language": "en",
        "release_date": "2010-01-01",
    }


class FakeTMDB:
    def __init__(self):
        self.calls: list[dict] = []

    async def search_keywords(self, query):
        return {"results": [{"id": 1, "name": query}]}

    async def get_discover(self, media_type, page=1, **kwargs):
        self.calls.append(kwargs)
        # Fewer votes allowed means a deeper pool, as on TMDB.
        base = page * 100 + (50 if kwargs.get("vote_count.gte", 999) < 400 else 0)
        return {"results": [result(base + n) for n in range(20)] + [result(base + 90, avg=5.1)]}


async def fake_fetch_batch(tmdb_service, items, media_type, user_settings=None):
    return [
        {"id": f"tt{i['id']}", "_tmdb_id": i["id"], "type": "movie", "vote_average": i["vote_average"]} for i in items
    ]


def test_a_row_holds_at_least_twenty_titles_above_the_floor(monkeypatch):
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))
    service = ContextRowService(FakeTMDB(), get_default_settings())

    out = asyncio.run(service.get_recommendations(f"{CONTEXT_PREFIX}spooky", "movie", None, set(), set(), 20))

    assert len(out) >= 20
    assert all(m["vote_average"] >= 6.5 for m in out)


def test_watched_titles_are_left_out(monkeypatch):
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))
    service = ContextRowService(FakeTMDB(), get_default_settings())

    out = asyncio.run(service.get_recommendations(f"{CONTEXT_PREFIX}spooky", "movie", None, {100, 101}, {"tt102"}, 20))

    assert {100, 101} & {m["_tmdb_id"] for m in out} == set()
    assert "tt102" not in {m["id"] for m in out}


def test_the_selector_offers_the_catalog_and_its_rows_map_back_to_it():
    from app.services.catalog_definitions import get_config_id

    defaults = get_default_settings()

    assert any(c.id == "watchly.ctx" and c.enabled for c in defaults.catalogs)
    assert get_config_id({"id": "watchly.ctx.2"}) == "watchly.ctx"


def test_on_this_day_is_for_movies_only():
    assert SPEC_BY_KEY["on_this_day"].types == ("movie",)
    assert "on_this_day" not in keys("series", at("2026-04-14"))


def test_rows_ask_tmdb_for_english_unless_they_are_about_somewhere_else():
    tmdb = FakeTMDB()
    monkeypatched = ContextRowService(tmdb, get_default_settings())

    async def run(key):
        tmdb.calls.clear()
        await monkeypatched.get_recommendations(f"{CONTEXT_PREFIX}{key}", "movie", None, set(), set(), 20)
        return {c.get("with_original_language") for c in tmdb.calls}

    assert asyncio.run(run("late_night")) == {"en"}
    assert asyncio.run(run("african")) == {None}


def test_hidden_gems_leave_out_popular_and_recent_titles(monkeypatch):
    tmdb = FakeTMDB()
    popular = {**result(7), "popularity": 400.0}
    obscure = {**result(8), "popularity": 12.0}

    async def discover(media_type, page=1, **kwargs):
        tmdb.calls.append(kwargs)
        return {"results": [popular, obscure]}

    tmdb.get_discover = discover
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))
    service = ContextRowService(tmdb, get_default_settings())

    out = asyncio.run(service.get_recommendations(f"{CONTEXT_PREFIX}hidden_gems", "series", None, set(), set(), 20))

    assert [m["_tmdb_id"] for m in out] == [8]
    assert all(c["first_air_date.lte"] <= "2024-10-27" for c in tmdb.calls)


def test_movies_always_lead_with_on_this_day_even_when_it_rains():
    for rain in (False, True):
        for count in (1, 3, 6):
            assert keys("movie", at("2026-10-23", rain=rain), count=count)[0] == "on_this_day"
    assert keys("movie", at("2026-10-23", rain=True), count=1) == ["on_this_day"]


def test_any_row_count_gives_that_many_distinct_rows():
    for content_type in ("movie", "series"):
        for count in range(1, 7):
            picked = keys(content_type, at("2026-10-23", rain=True), count=count)
            assert len(picked) == count == len(set(picked))


def test_changing_the_row_count_rebuilds_the_days_picks(world):
    _, three = build(CatalogConfig(id="watchly.ctx", rows=3))
    rows, five = build(CatalogConfig(id="watchly.ctx", rows=5))

    assert len(five["movie"]) == 5 and len(three["movie"]) == 3
    assert sum(r["type"] == "movie" for r in rows) == 5


def test_a_days_picks_from_before_the_rules_changed_are_chosen_again(world):
    build()
    import json

    stale = json.loads(next(iter(world.data.values())))
    stale["v"] = 1
    stale["picks"]["series"][1] = {"definition": "on_this_day", "name": "On This Day, 27 Oct"}
    world.data[next(iter(world.data))] = json.dumps(stale)

    _, slots = build()

    assert "on_this_day" not in slots["series"].values()
