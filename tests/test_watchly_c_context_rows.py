import asyncio
import json
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
    is_eligible,
)

TOKEN = "tok_ctx"


def at(day: str, rain: bool = False, people: bool = True) -> Today:
    return Today(date.fromisoformat(day), rain, people)


def keys(types: list[str], today_: Today, shown: dict[str, str] | None = None, count: int = 3) -> list[str]:
    return [s.key for s in choose_specs(types, today_, shown or {}, random.Random(1), count)]


BOTH = ["movie", "series"]


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
    for types in (["movie"], ["series"], BOTH):
        assert len(keys(types, at("2026-04-14"), count=4)) == 4


def test_a_type_only_gets_specs_that_suit_it():
    assert not {"people", "on_this_day"} & set(keys(["series"], at("2026-04-14"), count=6))
    assert "short_binges" not in keys(["movie"], at("2026-04-14"), count=6)


def test_no_row_repeats_across_movies_and_series():
    for count in range(1, 9):
        picked = keys(BOTH, at("2026-10-27", rain=True), count=count)
        assert len(picked) == len(set(picked)) == count


def test_the_rotating_slot_cycles_through_every_evergreen_row():
    seen: set[str] = set()
    shown: dict[str, str] = {}
    day = date(2026, 4, 14)
    for n in range(10):
        today_ = Today(day + timedelta(days=n), False, True)
        for spec in choose_specs(BOTH, today_, shown, random.Random(n), 4):
            shown[spec.key] = today_.day.isoformat()
            seen.add(spec.key)
    assert seen >= {s.key for s in SPECS if s.kind == "evergreen"}


def test_every_row_has_a_definition_for_each_type_it_claims():
    for spec in SPECS:
        if not spec.build:
            assert set(spec.types) <= set(spec.looks), spec.key


def test_weekday_rows_only_appear_on_their_day():
    for key, weekday in (("monday", "2026-11-02"), ("midweek", "2026-11-04"), ("thursday", "2026-11-05")):
        assert key in [s.key for s in SPECS if is_eligible(s, at(weekday))]
        assert key not in [s.key for s in SPECS if is_eligible(s, at("2026-11-03"))]


def test_payday_runs_from_the_25th_to_the_end_of_the_month():
    assert is_eligible(SPEC_BY_KEY["payday"], at("2026-10-25"))
    assert is_eligible(SPEC_BY_KEY["payday"], at("2026-10-31"))
    assert not is_eligible(SPEC_BY_KEY["payday"], at("2026-10-24"))


def test_a_new_row_is_picked_every_day_for_three_weeks():
    shown: dict[str, str] = {}
    picked: list[set[str]] = []
    for n in range(21):
        today_ = Today(date(2026, 4, 1) + timedelta(days=n), False, True)
        day = {s.key for s in choose_specs(BOTH, today_, shown, random.Random(n), 6)} - {"on_this_day"}
        shown.update({k: today_.day.isoformat() for k in day})
        picked.append(day)
    assert all(a != b for a, b in zip(picked, picked[1:]))
    assert len(set().union(*picked)) >= 20


def test_no_people_row_without_a_movie_to_build_it_from():
    assert "people" not in keys(["movie"], at("2026-04-14", people=False), {}, count=6)


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


def test_rows_are_numbered_slots_under_one_type_with_a_definition_each(world):
    rows, slots = build()

    assert [r["id"] for r in rows] == [f"{CONTEXT_PREFIX}{n}" for n in (1, 2, 3)]
    assert {r["type"] for r in rows} == {"movie"}
    assert set(slots) == {"movie"}
    assert list(slots["movie"]) == ["ctx.1", "ctx.2", "ctx.3"]
    assert slots["movie"]["ctx.1"] == "on_this_day"
    assert len(set(slots["movie"].values())) == 3


def test_a_days_picks_are_stable_across_builds(world):
    first, _ = build()
    second, _ = build()

    assert first == second


def test_a_disabled_catalog_or_type_gives_no_rows(world):
    assert build(CatalogConfig(id="watchly.ctx", enabled=False))[0] == []
    rows, slots = build(CatalogConfig(id="watchly.ctx", enabled_movie=False, rows=3))
    assert {r["type"] for r in rows} == {"series"}
    assert "on_this_day" not in slots["series"].values()


def test_a_new_day_moves_on_to_rows_not_shown_yet(world, monkeypatch):
    _, first = build()
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 28))
    _, second = build()

    assert first["movie"]["ctx.2"] != second["movie"]["ctx.2"]


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
    assert "on_this_day" not in keys(["series"], at("2026-04-14"), count=6)


def on_this_day(monkeypatch, tmdb, today_: date) -> list[dict]:
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(context_rows, "today", lambda: today_)
    service = ContextRowService(tmdb, get_default_settings())
    return asyncio.run(service.get_recommendations(f"{CONTEXT_PREFIX}on_this_day", "movie", None, set(), set(), 20))


def test_on_this_day_asks_for_the_exact_us_opening_day_of_every_year_since_1960(monkeypatch):
    tmdb = FakeTMDB()

    async def plenty(media_type, page=1, **kwargs):
        tmdb.calls.append(kwargs)
        return {"results": [result(len(tmdb.calls) * 100 + n) for n in range(20)]}

    tmdb.get_discover = plenty
    on_this_day(monkeypatch, tmdb, date(2026, 10, 30))

    first = tmdb.calls[:66]
    assert sorted(c["release_date.gte"] for c in first) == [f"{y}-10-30" for y in range(1960, 2026)]
    assert all(c["release_date.gte"] == c["release_date.lte"] for c in first)
    assert all(c["region"] == "US" and c["with_release_type"] == "2|3" for c in first)
    assert not any("primary_release_date.gte" in c for c in tmdb.calls)
    assert len(tmdb.calls) == 66


def test_on_this_day_widens_by_a_day_only_when_the_exact_day_is_short(monkeypatch):
    tmdb = FakeTMDB()

    async def discover(media_type, page=1, **kwargs):
        tmdb.calls.append(kwargs)
        exact = kwargs["release_date.gte"] == kwargs["release_date.lte"]
        year = int(kwargs["release_date.gte"][:4])
        if exact:
            return {"results": [result(year * 10, avg=7.0)] if year < 1970 else []}
        return {"results": [result(year * 10 + 1, avg=9.0)]}

    tmdb.get_discover = discover
    out = on_this_day(monkeypatch, tmdb, date(2026, 10, 30))

    ids = [m["_tmdb_id"] for m in out]
    exact_ids = {y * 10 for y in range(1960, 1970)}
    assert set(ids[: len(exact_ids)]) == exact_ids
    assert len(ids) > len(exact_ids)
    spans = {(c["release_date.gte"], c["release_date.lte"]) for c in tmdb.calls if c["release_date.gte"][:4] == "2000"}
    assert ("2000-10-30", "2000-10-30") in spans and ("2000-10-29", "2000-10-31") in spans


def test_on_this_day_on_leap_day_uses_the_28th_in_years_without_one(monkeypatch):
    tmdb = FakeTMDB()
    on_this_day(monkeypatch, tmdb, date(2028, 2, 29))

    days = {c["release_date.gte"] for c in tmdb.calls[:68]}
    assert "1961-02-28" in days and "1964-02-29" in days


def popularity_rows(monkeypatch, popularity: str, key: str) -> set[int]:
    tmdb = FakeTMDB()
    thin = result(1, avg=8.0, votes=120)
    solid = result(2, avg=7.6, votes=900)

    async def discover(media_type, page=1, **kwargs):
        return {"results": [thin, solid]}

    tmdb.get_discover = discover
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))
    service = ContextRowService(tmdb, get_default_settings().model_copy(update={"popularity": popularity}))
    out = asyncio.run(service.get_recommendations(f"{CONTEXT_PREFIX}{key}", "movie", None, set(), set(), 20))
    return {m["_tmdb_id"] for m in out}


def test_the_popularity_setting_applies_to_contextual_rows(monkeypatch):
    assert popularity_rows(monkeypatch, "balanced", "spooky") == {2}
    assert popularity_rows(monkeypatch, "all", "spooky") == {1, 2}


def test_the_rows_about_titles_with_few_votes_ignore_it(monkeypatch):
    assert popularity_rows(monkeypatch, "balanced", "african") == {1, 2}


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
            assert keys(BOTH, at("2026-10-23", rain=rain), count=count)[0] == "on_this_day"
    assert keys(BOTH, at("2026-10-23", rain=True), count=1) == ["on_this_day"]


def test_changing_the_row_count_rebuilds_the_days_picks(world):
    _, three = build(CatalogConfig(id="watchly.ctx", rows=3))
    rows, five = build(CatalogConfig(id="watchly.ctx", rows=5))

    assert len(five["movie"]) == 5 and len(three["movie"]) == 3
    assert len(rows) == 5


def test_a_days_picks_from_before_the_rules_changed_are_chosen_again(world):
    build()
    stale = json.loads(next(iter(world.data.values())))
    stale["v"] = 1
    stale["picks"] = [{"definition": "spooky", "name": "Spooky Season"}] * 3
    world.data[next(iter(world.data))] = json.dumps(stale)

    _, slots = build()

    assert list(slots["movie"].values())[0] == "on_this_day"


def _row_states():
    return {t: (None, set(), set()) for t in BOTH}


def test_a_row_blends_movies_and_series_card_by_card(monkeypatch):
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_typed_batch)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))
    service = ContextRowService(FakeTMDB(), get_default_settings())

    out = asyncio.run(service.get_mixed(f"{CONTEXT_PREFIX}late_night", BOTH, _row_states(), {}, 20))

    assert len(out) == 20
    assert [m["type"] for m in out[:6]] == ["movie", "series"] * 3


def test_a_movie_only_row_stays_movies_and_claimed_titles_are_left_out(monkeypatch):
    monkeypatch.setattr(context_rows.RecommendationMetadata, "fetch_batch", fake_typed_batch)
    monkeypatch.setattr(context_rows, "today", lambda: date(2026, 10, 27))
    service = ContextRowService(FakeTMDB(), get_default_settings())

    out = asyncio.run(service.get_mixed(f"{CONTEXT_PREFIX}on_this_day", BOTH, _row_states(), {"movie": {100, 101}}, 20))

    assert {m["type"] for m in out} == {"movie"}
    assert not {100, 101} & {m["_tmdb_id"] for m in out}


async def fake_typed_batch(tmdb_service, items, media_type, user_settings=None):
    kind = "series" if media_type in ("tv", "series") else "movie"
    return [{"id": f"tt{kind}{i['id']}", "_tmdb_id": i["id"], "type": kind} for i in items]
