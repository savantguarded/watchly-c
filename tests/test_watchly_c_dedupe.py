import asyncio
import json

import pytest

from app.core.constants import CLAIMS_KEY
from app.watchly_c import dedupe
from app.watchly_c.dedupe import build_order, claimed_by_earlier_rows, priority, record_claims

TOKEN = "tok_dedupe"


def key(content_type: str, row: str) -> str:
    return CLAIMS_KEY.format(token=TOKEN, content_type=content_type, catalog_id=row)


def test_seed_rows_outrank_seasonal_which_outrank_genre_rows_then_the_rest():
    order = ["watchly.rec", "watchly.theme.1", "watchly.ctx.1", "watchly.item.mix.2", "watchly.item.mix.1"]

    ranked = [c["id"] for c in build_order([{"id": i} for i in order])]

    assert ranked == ["watchly.item.mix.1", "watchly.item.mix.2", "watchly.ctx.1", "watchly.theme.1", "watchly.rec"]


def test_rewatch_and_unknown_ids_never_take_part():
    assert priority("watchly.rewatch") is None
    assert priority("watchly.item.tt123") is None


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.asked: list[str] = []

    async def mget(self, keys):
        self.asked = keys
        return [self.data.get(k) for k in keys]


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get_client(self):
        return FakeClient(self.data)

    async def set(self, k, value, ttl=None):
        self.data[k] = value
        return True

    async def delete(self, k):
        self.data.pop(k, None)
        return True


@pytest.fixture
def fake(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(dedupe, "redis_service", redis)
    return redis


def test_a_row_sees_only_what_earlier_rows_claimed(fake):
    fake.data[key("movie", "watchly.item.mix.1")] = json.dumps({"movie": [1, 2], "series": [9]})
    fake.data[key("movie", "watchly.theme.1")] = json.dumps({"movie": [3], "series": []})

    claimed = asyncio.run(claimed_by_earlier_rows(TOKEN, "watchly.ctx.1"))

    assert claimed == {"movie": {1, 2}, "series": {9}}


def test_the_first_row_has_nothing_to_look_up(fake):
    assert asyncio.run(claimed_by_earlier_rows(TOKEN, "watchly.item.mix.1")) == {"movie": set(), "series": set()}


def test_recording_replaces_the_old_claim_and_an_empty_row_drops_it(fake):
    metas = [{"_tmdb_id": 5, "type": "movie"}, {"_tmdb_id": 6, "type": "series"}]
    asyncio.run(record_claims(TOKEN, "movie", "watchly.ctx.2", metas))
    assert json.loads(fake.data[key("movie", "watchly.ctx.2")]) == {"movie": [5], "series": [6]}

    asyncio.run(record_claims(TOKEN, "movie", "watchly.ctx.2", []))
    assert key("movie", "watchly.ctx.2") not in fake.data
