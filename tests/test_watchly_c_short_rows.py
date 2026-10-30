import asyncio

from app.core.constants import DEFAULT_CATALOG_LIMIT
from app.core.settings import get_default_settings
from app.services.recommendation import catalog_service as cs_module
from app.services.recommendation.catalog_service import catalog_service
from app.watchly_c.short_rows import SHORT_ROW_TTL_SECONDS, top_up


def meta(n: int, kind: str = "movie") -> dict:
    return {"id": f"tt{kind}{n}", "type": kind, "name": f"Title {n}", "_tmdb_id": n}


def fill(first: int, count: int) -> list[dict]:
    return [meta(n) for n in range(first, first + count)]


def run_top_up(row, claimed, pool, asked=None):
    async def more(taken, limit):
        if asked is not None:
            asked.append((taken, limit))
        return [m for m in pool if m["_tmdb_id"] not in taken.get(m["type"], set())]

    return asyncio.run(top_up(row, claimed, more))


def test_a_full_row_is_left_alone():
    row = fill(1, DEFAULT_CATALOG_LIMIT)
    asked: list = []

    assert run_top_up(row, {}, fill(100, 30), asked) == row
    assert asked == []


def test_a_short_row_is_filled_to_twenty_without_repeating_what_it_or_other_rows_hold():
    row = fill(1, 12)
    asked: list = []

    out = run_top_up(row, {"movie": {100, 101}}, fill(1, 12) + fill(100, 30), asked)

    assert len(out) == DEFAULT_CATALOG_LIMIT and out[:12] == row
    ids = [m["id"] for m in out]
    assert len(set(ids)) == DEFAULT_CATALOG_LIMIT
    assert not {100, 101} & {m["_tmdb_id"] for m in out[12:]}
    assert asked[0][0]["movie"] >= set(range(1, 13)) | {100, 101}


def test_a_title_held_under_a_different_tmdb_id_is_still_not_repeated():
    row = fill(1, 19)
    twin = {**meta(500), "id": row[0]["id"]}

    out = run_top_up(row, {}, [twin, meta(501)])

    assert [m["id"] for m in out].count(row[0]["id"]) == 1 and len(out) == 20


def test_a_failed_top_up_keeps_the_short_row():
    row = fill(1, 5)

    async def broken(taken, limit):
        raise TimeoutError

    assert asyncio.run(top_up(row, {}, broken)) == row


def test_a_row_the_top_up_cannot_complete_comes_back_short():
    assert len(run_top_up(fill(1, 5), {}, fill(100, 3))) == 8


def build_row(monkeypatch, own: list[dict], pool: list[dict], catalog_id="watchly.theme.1"):
    stored = {}

    async def resolve(token, content_type, requested):
        return requested

    async def profile(token, content_type):
        return None, set(), set()

    calls = []

    async def recommendations(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return list(own)
        return [m for m in pool if m["_tmdb_id"] not in kwargs["claimed"]["movie"]]

    async def remember(token, content_type, requested, data, ttl):
        stored.update(metas=data["metas"], ttl=ttl)

    monkeypatch.setattr(catalog_service, "_resolve_slot", resolve)
    monkeypatch.setattr(catalog_service, "_initialize_services", lambda settings: {"profile": None})
    monkeypatch.setattr(catalog_service, "_get_recommendations", recommendations)
    monkeypatch.setattr(cs_module.user_cache, "get_profile_and_watched_sets", profile)
    monkeypatch.setattr(cs_module.user_cache, "set_catalog", remember)

    class Ctx:
        token = "tok_short"
        library = bundle = auth_key = None
        user_settings = get_default_settings()

    asyncio.run(catalog_service._build_catalog(Ctx(), "movie", catalog_id, {}))
    return stored


def test_a_short_row_is_topped_up_and_cached_for_a_week_once_full(monkeypatch):
    stored = build_row(monkeypatch, fill(1, 12), fill(100, 30))

    assert len(stored["metas"]) == DEFAULT_CATALOG_LIMIT
    assert stored["ttl"] == cs_module.settings.CATALOG_STALE_TTL


def test_a_row_that_stays_short_is_cached_briefly(monkeypatch):
    stored = build_row(monkeypatch, fill(1, 12), fill(100, 3))

    assert len(stored["metas"]) == 15
    assert stored["ttl"] == SHORT_ROW_TTL_SECONDS


def test_because_you_watched_rows_are_not_topped_up_with_unrelated_titles(monkeypatch):
    for catalog_id in ("watchly.item.1", "watchly.item.mix.movie.tt0325980"):
        assert len(build_row(monkeypatch, fill(1, 12), fill(100, 30), catalog_id)["metas"]) == 12


def test_top_picks_and_watch_it_again_are_not_topped_up(monkeypatch):
    assert len(build_row(monkeypatch, fill(1, 12), fill(100, 30), "watchly.rewatch")["metas"]) == 12
    assert len(build_row(monkeypatch, fill(1, 12), fill(100, 30), "watchly.rec")["metas"]) == 12


def test_the_switch_turns_the_top_up_off(monkeypatch):
    monkeypatch.setattr(cs_module.settings, "FULL_ROWS", False)
    stored = build_row(monkeypatch, fill(1, 12), fill(100, 30))

    assert len(stored["metas"]) == 12 and stored["ttl"] == cs_module.settings.CATALOG_STALE_TTL
