import asyncio
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.app import app
from app.core.settings import get_default_settings
from app.services.recommendation import catalog_service as cs_module
from app.services.recommendation.catalog_service import catalog_service
from app.services.recommendation.catalog_utils import shuffle_data_if_needed
from app.watchly_c import daily_refresh, shuffle
from app.watchly_c.shuffle import timed_shuffle


class FakeRedis:
    def __init__(self, tokens=()):
        self.data: dict[str, str] = {f"watchly:token:{t}": "{}" for t in tokens}
        self.ttls: dict[str, int | None] = {}

    async def set_nx(self, key, value, ttl=None):
        if key in self.data:
            return False
        self.data[key] = value
        self.ttls[key] = ttl
        return True

    async def set(self, key, value, ttl=None):
        self.data[key] = value
        self.ttls[key] = ttl
        return True

    async def get_client(self):
        return self

    async def scan_iter(self, match, count=None):
        prefix = match.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key


@pytest.fixture
def setup(monkeypatch):
    redis = FakeRedis(tokens=("nuvio-acct", "stremio-acct"))
    accounts = {
        "nuvio-acct": {"settings": {**get_default_settings().model_dump(), "watch_history_source": "nuvio"}},
        "stremio-acct": {"settings": get_default_settings().model_dump()},
    }
    calls: list[tuple[str, str]] = []

    async def get_user_data(token):
        return accounts.get(token)

    async def resolve_auth_key(bundle, credentials, token):
        return None

    async def cache_library(bundle, auth_key, user_settings, token):
        calls.append(("library", token))

    async def refresh(token, credentials):
        calls.append(("rows", token))
        return True

    monkeypatch.setattr(daily_refresh, "redis_service", redis)
    monkeypatch.setattr(daily_refresh.token_store, "get_user_data", get_user_data)
    monkeypatch.setattr(daily_refresh.auth_service, "resolve_auth_key_with_bundle", resolve_auth_key)
    monkeypatch.setattr(daily_refresh.manifest_service, "cache_library_and_profiles", cache_library)
    monkeypatch.setattr(daily_refresh.catalog_updater, "refresh_catalogs_for_credentials", refresh)
    monkeypatch.setattr(daily_refresh, "today", lambda: date(2026, 10, 22))
    return redis, calls


def test_every_account_is_refreshed_and_external_libraries_are_refetched(setup):
    _, calls = setup

    response = TestClient(app).get("/watchly-c/daily-refresh")

    assert response.json() == {"day": "2026-10-22", "status": "done", "refreshed": 2, "failed": 0, "deferred": 0}
    assert ("library", "nuvio-acct") in calls
    # The stock refresh already refetches a Stremio library.
    assert ("library", "stremio-acct") not in calls
    assert {("rows", "nuvio-acct"), ("rows", "stremio-acct")} <= set(calls)


def test_it_runs_once_a_day_however_often_it_is_called(setup):
    _, calls = setup
    client = TestClient(app)
    client.get("/watchly-c/daily-refresh")
    calls.clear()

    assert client.get("/watchly-c/daily-refresh").json()["status"] == "already-ran"
    assert calls == []


def test_only_a_clean_run_holds_the_day_and_an_unfinished_one_frees_itself_soon(setup, monkeypatch):
    redis, _ = setup
    run_key = "watchly:c:daily_refresh:run:2026-10-22"
    monkeypatch.setattr(daily_refresh, "TIME_BUDGET_SECONDS", -1)
    TestClient(app).get("/watchly-c/daily-refresh")
    assert redis.ttls[run_key] == daily_refresh.RUN_LOCK_TTL_SECONDS

    monkeypatch.setattr(daily_refresh, "TIME_BUDGET_SECONDS", 200)
    redis.data.pop(run_key)
    TestClient(app).get("/watchly-c/daily-refresh")
    assert redis.ttls[run_key] == daily_refresh.RUN_DONE_TTL_SECONDS


def test_the_cron_secret_is_enforced_once_set(setup, monkeypatch):
    monkeypatch.setattr(daily_refresh.settings, "CRON_SECRET", "s3cret-value-123")
    client = TestClient(app)

    assert client.get("/watchly-c/daily-refresh").status_code == 401
    assert client.get("/watchly-c/daily-refresh", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/watchly-c/daily-refresh", headers={"Authorization": "Bearer s3cret-value-123"})
    assert ok.json()["status"] == "done"


def test_one_failing_account_does_not_stop_the_rest(setup, monkeypatch):
    _, calls = setup

    async def broken(bundle, auth_key, user_settings, token):
        raise RuntimeError("nuvio down")

    monkeypatch.setattr(daily_refresh.manifest_service, "cache_library_and_profiles", broken)

    result = asyncio.run(daily_refresh.refresh_all_accounts())

    assert result == {"refreshed": 1, "failed": 1, "deferred": 0}
    assert ("rows", "stremio-acct") in calls


def test_accounts_past_the_time_budget_are_left_for_later(setup, monkeypatch):
    monkeypatch.setattr(daily_refresh, "TIME_BUDGET_SECONDS", -1)

    assert asyncio.run(daily_refresh.refresh_all_accounts()) == {"refreshed": 0, "failed": 0, "deferred": 2}


def rewatch_settings(**changes):
    user_settings = get_default_settings()
    config = next(c for c in user_settings.catalogs if c.id == "watchly.rewatch")
    for field, value in {"enabled": True, **changes}.items():
        setattr(config, field, value)
    return user_settings


@pytest.fixture
def rebuilds(monkeypatch):
    built: list[tuple[str, str]] = []

    class Ctx:
        async def close(self):
            built.append(("closed", ""))

    async def load(token):
        return Ctx()

    async def build(ctx, content_type, catalog_id, headers):
        built.append((content_type, catalog_id))

    monkeypatch.setattr(daily_refresh, "load_user_context", load)
    monkeypatch.setattr(daily_refresh.catalog_service, "_build_catalog", build)
    return built


def test_the_rewatch_row_is_rebuilt_for_each_enabled_type(rebuilds):
    asyncio.run(daily_refresh.rebuild_rewatch("tok", rewatch_settings(enabled_series=False)))

    assert rebuilds == [("movie", "watchly.rewatch"), ("closed", "")]


def test_a_disabled_rewatch_row_is_left_alone(rebuilds):
    asyncio.run(daily_refresh.rebuild_rewatch("tok", get_default_settings()))

    assert rebuilds == []


def test_the_refresh_rebuilds_rewatch_after_the_manifest(setup, rebuilds, monkeypatch):
    monkeypatch.setattr(daily_refresh, "extract_settings", lambda credentials: rewatch_settings())

    asyncio.run(daily_refresh.refresh_account("stremio-acct", {}))

    assert rebuilds == [("movie", "watchly.rewatch"), ("series", "watchly.rewatch"), ("closed", "")]


def test_the_refresh_leaves_rewatch_alone_when_the_switch_is_off(setup, rebuilds, monkeypatch):
    monkeypatch.setattr(daily_refresh.settings, "DAILY_REWATCH", False)
    monkeypatch.setattr(daily_refresh, "extract_settings", lambda credentials: rewatch_settings())

    asyncio.run(daily_refresh.refresh_account("stremio-acct", {}))

    assert rebuilds == []


def shuffled_settings(catalog_id: str):
    user_settings = get_default_settings()
    next(c for c in user_settings.catalogs if c.id == catalog_id).shuffle = True
    return user_settings


ROW = [{"id": f"tt{n}"} for n in range(20)]


def test_a_shuffled_row_keeps_its_order_within_the_window(monkeypatch):
    monkeypatch.setattr(shuffle, "shuffle_period", lambda: "2026-10-22:14")
    user_settings = shuffled_settings("watchly.rec")

    first = timed_shuffle(user_settings, "watchly.rec", list(ROW))

    assert first != ROW
    assert timed_shuffle(user_settings, "watchly.rec", list(ROW)) == first


def test_a_shuffled_row_changes_order_with_the_window(monkeypatch):
    user_settings = shuffled_settings("watchly.rec")
    monkeypatch.setattr(shuffle, "shuffle_period", lambda: "2026-10-22:14")
    two_pm = timed_shuffle(user_settings, "watchly.rec", list(ROW))
    monkeypatch.setattr(shuffle, "shuffle_period", lambda: "2026-10-22:15")

    assert timed_shuffle(user_settings, "watchly.rec", list(ROW)) != two_pm


def test_the_window_follows_the_interval(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 22, 13, 30, tzinfo=timezone.utc)  # 14:30 in Lagos

    monkeypatch.setattr(shuffle, "datetime", Clock)
    monkeypatch.setattr(shuffle.settings, "SHUFFLE_INTERVAL_HOURS", 1)
    assert shuffle.shuffle_period() == "2026-10-22:14"
    monkeypatch.setattr(shuffle.settings, "SHUFFLE_INTERVAL_HOURS", 6)
    assert shuffle.shuffle_period() == "2026-10-22:2"


def test_slot_rows_follow_their_catalogs_shuffle_setting():
    """Served ids are slots, which never matched a config entry before."""
    assert timed_shuffle(shuffled_settings("watchly.theme"), "watchly.theme.2", list(ROW)) != ROW
    assert timed_shuffle(shuffled_settings("watchly.item"), "watchly.item.mix.tt1", list(ROW)) != ROW


def test_rows_without_shuffle_are_left_alone():
    assert timed_shuffle(get_default_settings(), "watchly.rec", list(ROW)) == ROW


def test_a_freshly_built_row_is_served_in_the_same_order_as_its_cache_hits(monkeypatch):
    monkeypatch.setattr(shuffle, "shuffle_period", lambda: "2026-10-22:14")
    user_settings = shuffled_settings("watchly.item")
    stored = {}

    async def no_slot_change(token, content_type, catalog_id):
        return catalog_id

    async def cached_profile(token, content_type):
        return None, set(), set()

    async def recommendations(**kwargs):
        return [{**meta, "type": "movie", "name": meta["id"]} for meta in ROW]

    async def remember(token, content_type, catalog_id, data, ttl):
        stored["metas"] = list(data["metas"])

    monkeypatch.setattr(catalog_service, "_resolve_slot", no_slot_change)
    monkeypatch.setattr(catalog_service, "_initialize_services", lambda settings: {"profile": None})
    monkeypatch.setattr(catalog_service, "_get_recommendations", recommendations)
    monkeypatch.setattr(cs_module.user_cache, "get_profile_and_watched_sets", cached_profile)
    monkeypatch.setattr(cs_module.user_cache, "set_catalog", remember)

    class Ctx:
        token = "tok_shuffle"
        library = bundle = auth_key = None

    Ctx.user_settings = user_settings
    built, _ = asyncio.run(catalog_service._build_catalog(Ctx(), "movie", "watchly.item.mix.1", {}))
    hit = shuffle_data_if_needed(user_settings, "watchly.item.mix.1", list(stored["metas"]))

    assert [m["id"] for m in built["metas"]] == [m["id"] for m in hit]
    assert [m["id"] for m in stored["metas"]] == [m["id"] for m in ROW]
