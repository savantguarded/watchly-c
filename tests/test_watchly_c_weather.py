import asyncio
from datetime import date

import httpx

from app.watchly_c import weather

DAY = date(2026, 10, 27)


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value
        return True


class FakeClient:
    instances: list["FakeClient"] = []
    payload: dict | Exception = {}

    def __init__(self, **kwargs):
        self.closed = False
        self.instances.append(self)

    async def get(self, path, params=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def close(self):
        self.closed = True


def lookup(monkeypatch, payload) -> tuple[bool, FakeRedis]:
    redis = FakeRedis()
    FakeClient.instances = []
    FakeClient.payload = payload
    monkeypatch.setattr(weather, "redis_service", redis)
    monkeypatch.setattr(weather, "BaseClient", FakeClient)
    return asyncio.run(weather.rains_today(DAY)), redis


def test_forecast_rain_over_the_threshold_counts(monkeypatch):
    rain, _ = lookup(monkeypatch, {"daily": {"precipitation_sum": [6.4]}})
    assert rain


def test_a_drizzle_does_not(monkeypatch):
    rain, _ = lookup(monkeypatch, {"daily": {"precipitation_sum": [0.4]}})
    assert not rain


def test_a_failed_lookup_is_dry_and_kept_so_it_is_not_retried_by_every_account(monkeypatch):
    rain, redis = lookup(monkeypatch, httpx.ConnectError("down"))

    assert not rain
    assert list(redis.data.values()) == ["0"]
    assert FakeClient.instances[0].closed


def test_the_answer_is_looked_up_once_a_day(monkeypatch):
    _, redis = lookup(monkeypatch, {"daily": {"precipitation_sum": [9]}})
    FakeClient.instances = []

    assert asyncio.run(weather.rains_today(DAY))
    assert FakeClient.instances == []
