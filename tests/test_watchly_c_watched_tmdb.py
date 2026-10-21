import asyncio

import pytest

from app.core.constants import IMDB_TO_TMDB_KEY
from app.core.settings import get_default_settings
from app.models.library import LibraryCollection, StremioLibraryItem
from app.watchly_c import watched_tmdb as module
from app.watchly_c.watched_tmdb import watched_tmdb_ids

FIND = {"tt1": (101, "movie"), "tt2": (202, "tv"), "tt3": (303, "movie")}


class FakeHashRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    async def get_client(self):
        return self

    async def hmget(self, key, fields):
        h = self.hashes.get(key, {})
        return [h.get(f) for f in fields]

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)


class FakeTMDB:
    def __init__(self):
        self.lookups: list[str] = []

    async def find_by_imdb_id(self, imdb_id):
        self.lookups.append(imdb_id)
        return FIND.get(imdb_id, (None, None))


@pytest.fixture
def env(monkeypatch):
    redis, tmdb = FakeHashRedis(), FakeTMDB()
    monkeypatch.setattr(module, "redis_service", redis)
    monkeypatch.setattr(module, "get_tmdb_service", lambda language, api_key: tmdb)
    return redis, tmdb


def item(item_id: str, mtype: str) -> StremioLibraryItem:
    return StremioLibraryItem(_id=item_id, type=mtype, name=item_id, temp=False, removed=False)


LIBRARY = LibraryCollection(
    watched=[item("tt1", "movie"), item("tt2", "series"), item("tt3", "movie"), item("tt404", "movie")],
    source="nuvio",
)


def ids(content_type: str) -> set[int]:
    return asyncio.run(watched_tmdb_ids(LIBRARY, content_type, get_default_settings()))


def test_watched_imdb_ids_resolve_to_tmdb_ids_of_the_right_type(env):
    assert ids("movie") == {101, 303}
    assert ids("series") == {202}


def test_each_title_is_looked_up_once(env):
    _, tmdb = env
    ids("movie")
    tmdb.lookups.clear()

    ids("movie")

    assert tmdb.lookups == ["tt404"]


def test_misses_are_not_stored(env):
    redis, _ = env
    ids("movie")

    assert "tt404" not in redis.hashes[IMDB_TO_TMDB_KEY]
    assert redis.hashes[IMDB_TO_TMDB_KEY]["tt1"] == "movie:101"
