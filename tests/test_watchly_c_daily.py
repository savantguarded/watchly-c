import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.core.app import app
from app.core.settings import get_default_settings
from app.watchly_c import daily_refresh, shuffle
from app.watchly_c.shuffle import daily_shuffle


class FakeRedis:
    def __init__(self, tokens=()):
        self.data: dict[str, str] = {f"watchly:token:{t}": "{}" for t in tokens}

    async def set_nx(self, key, value, ttl=None):
        if key in self.data:
            return False
        self.data[key] = value
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


def shuffled_settings(catalog_id: str):
    user_settings = get_default_settings()
    next(c for c in user_settings.catalogs if c.id == catalog_id).shuffle = True
    return user_settings


ROW = [{"id": f"tt{n}"} for n in range(20)]


def test_a_shuffled_row_keeps_its_order_all_day(monkeypatch):
    monkeypatch.setattr(shuffle, "today", lambda: date(2026, 10, 22))
    user_settings = shuffled_settings("watchly.rec")

    first = daily_shuffle(user_settings, "watchly.rec", list(ROW))

    assert first != ROW
    assert daily_shuffle(user_settings, "watchly.rec", list(ROW)) == first


def test_a_shuffled_row_changes_order_with_the_day(monkeypatch):
    user_settings = shuffled_settings("watchly.rec")
    monkeypatch.setattr(shuffle, "today", lambda: date(2026, 10, 22))
    monday = daily_shuffle(user_settings, "watchly.rec", list(ROW))
    monkeypatch.setattr(shuffle, "today", lambda: date(2026, 10, 23))

    assert daily_shuffle(user_settings, "watchly.rec", list(ROW)) != monday


def test_slot_rows_follow_their_catalogs_shuffle_setting():
    """Served ids are slots, which never matched a config entry before."""
    assert daily_shuffle(shuffled_settings("watchly.theme"), "watchly.theme.2", list(ROW)) != ROW
    assert daily_shuffle(shuffled_settings("watchly.item"), "watchly.item.mix.tt1", list(ROW)) != ROW


def test_rows_without_shuffle_are_left_alone():
    assert daily_shuffle(get_default_settings(), "watchly.rec", list(ROW)) == ROW
