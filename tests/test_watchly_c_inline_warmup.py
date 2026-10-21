import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints import status as status_endpoint
from app.api.endpoints import tokens as tokens_endpoint
from app.core.app import app
from app.services import warmup as warmup_module
from app.services.warmup import WARM_LOCK_KEY, WARM_STATUS_KEY
from app.watchly_c import inline_warmup

TOKEN = "tok_inline"


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value
        return True

    async def set_nx(self, key, value, ttl=None):
        if key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def env(monkeypatch):
    redis = FakeRedis()
    primed: list[str] = []

    async def prime(token, auth_key, user_settings):
        if not await redis.set_nx(WARM_LOCK_KEY.format(token=token), "1"):
            return
        primed.append(token)
        await redis.set(WARM_STATUS_KEY.format(token=token), json.dumps({"state": "ready"}))
        await redis.delete(WARM_LOCK_KEY.format(token=token))

    async def get_user_data(token):
        return {"settings": {}} if token == TOKEN else None

    async def resolve_alias(token):
        return token

    async def no_auth_key(bundle, credentials, token):
        return None

    monkeypatch.setattr(warmup_module, "redis_service", redis)
    monkeypatch.setattr(inline_warmup, "redis_service", redis)
    monkeypatch.setattr(inline_warmup.warmup_service, "prime", prime)
    monkeypatch.setattr(inline_warmup.token_store, "get_user_data", get_user_data)
    monkeypatch.setattr(status_endpoint.token_store, "resolve_alias", resolve_alias)
    monkeypatch.setattr(inline_warmup.auth_service, "resolve_auth_key_with_bundle", no_auth_key)
    monkeypatch.setattr(status_endpoint.settings, "INLINE_WARMUP", True)
    return redis, primed


def pending(redis):
    redis.data[WARM_STATUS_KEY.format(token=TOKEN)] = json.dumps({"state": "pending"})


def test_the_poll_that_finds_pending_runs_the_warm_up(env):
    redis, primed = env
    pending(redis)

    assert TestClient(app).get(f"/{TOKEN}/status").json()["state"] == "ready"
    assert primed == [TOKEN]


def test_later_polls_do_no_work(env):
    redis, primed = env
    pending(redis)
    client = TestClient(app)
    client.get(f"/{TOKEN}/status")

    client.get(f"/{TOKEN}/status")

    assert primed == [TOKEN]


def test_a_lock_left_by_a_frozen_warm_up_is_cleared(env):
    redis, primed = env
    pending(redis)
    redis.data[WARM_LOCK_KEY.format(token=TOKEN)] = "1"

    assert TestClient(app).get(f"/{TOKEN}/status").json()["state"] == "ready"


def test_off_vercel_the_poll_stays_cheap(env, monkeypatch):
    redis, primed = env
    pending(redis)
    monkeypatch.setattr(status_endpoint.settings, "INLINE_WARMUP", False)

    assert TestClient(app).get(f"/{TOKEN}/status").json()["state"] == "pending"
    assert primed == []


def test_saving_does_not_start_a_background_warm_up_when_inline(env, monkeypatch):
    enqueued = []
    monkeypatch.setattr(tokens_endpoint.warmup_service, "enqueue", lambda *a: enqueued.append(a))

    async def create(payload):
        return type("R", (), {"token": TOKEN})(), None, None

    async def mark_pending(token):
        return None

    monkeypatch.setattr(tokens_endpoint.auth_service, "create_user_token", create)
    monkeypatch.setattr(tokens_endpoint.warmup_service, "mark_pending", mark_pending)
    monkeypatch.setattr(tokens_endpoint.settings, "INLINE_WARMUP", True)

    asyncio.run(tokens_endpoint.create_token(None))
    assert enqueued == []
