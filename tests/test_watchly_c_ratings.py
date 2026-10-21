import asyncio
from datetime import datetime, timezone

import httpx

from app.core.settings import get_default_settings
from app.models.history import WatchHistory, WatchHistoryItem
from app.models.library import LibraryCollection
from app.services import context
from app.services.profile import service as profile_service_module
from app.services.stremio.library import watch_history_to_library_collection
from app.watchly_c import ratings as ratings_module
from app.watchly_c.ratings import add_mdblist_ratings

KEY = "good-key"
RATED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def nuvio_item(imdb_id: str, mtype: str = "movie") -> WatchHistoryItem:
    return WatchHistoryItem(imdb_id=imdb_id, type=mtype, name=imdb_id, source="nuvio")


def rated(imdb_id: str, rating: float, mtype: str = "movie") -> WatchHistoryItem:
    return WatchHistoryItem(
        imdb_id=imdb_id,
        type=mtype,
        name=imdb_id,
        rating=rating,
        watch_count=0,
        completion=0.0,
        last_watched=RATED_AT,
        rated_at=RATED_AT,
        source="mdblist",
    )


class FakeMDBList:
    def __init__(self, ratings: dict[str, WatchHistoryItem] | None = None, status: int | None = None):
        self.ratings = ratings or {}
        self.status = status

    async def get_ratings(self, api_key: str) -> dict[str, WatchHistoryItem]:
        if api_key != KEY:
            self.status = 401
        if self.status:
            request = httpx.Request("GET", "https://api.mdblist.com/sync/ratings")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(self.status, request=request))
        return dict(self.ratings)


def test_ratings_land_on_watched_titles_and_rated_only_titles_are_added(monkeypatch):
    fake = FakeMDBList({"tt1": rated("tt1", 9.5), "tt9": rated("tt9", 8.0, "series")})
    monkeypatch.setattr(ratings_module, "mdblist_service", fake)
    history = WatchHistory(items=[nuvio_item("tt1"), nuvio_item("tt2")], source="nuvio")

    merged = asyncio.run(add_mdblist_ratings(history, KEY))

    by_id = {i.imdb_id: i for i in merged.items}
    assert merged.source == "nuvio"
    assert by_id["tt1"].rating == 9.5 and by_id["tt1"].rated_at == RATED_AT
    assert by_id["tt2"].rating is None
    assert by_id["tt9"].source == "nuvio" and by_id["tt9"].type == "series"


def test_ratings_reach_the_library_buckets(monkeypatch):
    fake = FakeMDBList({"tt1": rated("tt1", 9.5), "tt2": rated("tt2", 7.5)})
    monkeypatch.setattr(ratings_module, "mdblist_service", fake)
    history = WatchHistory(items=[nuvio_item("tt1"), nuvio_item("tt2"), nuvio_item("tt3")], source="nuvio")

    library = watch_history_to_library_collection(asyncio.run(add_mdblist_ratings(history, KEY)))

    assert [i.id for i in library.loved] == ["tt1"]
    assert [i.id for i in library.liked] == ["tt2"]
    assert [i.id for i in library.watched] == ["tt3"]
    assert library.loved[0].rating == 9.5 and library.loved[0].rated_at == RATED_AT
    assert library.source == "nuvio"


def test_an_mdblist_outage_falls_back_to_nuvio_alone(monkeypatch):
    monkeypatch.setattr(ratings_module, "mdblist_service", FakeMDBList(status=503))
    history = WatchHistory(items=[nuvio_item("tt1")], source="nuvio")

    assert asyncio.run(add_mdblist_ratings(history, KEY)) is history


def test_a_rejected_key_falls_back_to_nuvio_alone(monkeypatch):
    monkeypatch.setattr(ratings_module, "mdblist_service", FakeMDBList({"tt1": rated("tt1", 10)}))
    history = WatchHistory(items=[nuvio_item("tt1")], source="nuvio")

    merged = asyncio.run(add_mdblist_ratings(history, "wrong-key"))

    assert merged.items[0].rating is None


class ExternalSourceReturning:
    def __init__(self, library):
        self.library = library

    async def fetch_external_library(self, source, user_settings, token=None):
        return self.library


def test_an_empty_external_history_is_not_cached_as_the_library(monkeypatch):
    """A Nuvio profile reporting nothing watched looks like a blank account; caching
    that would empty every row until the cache next expired."""
    monkeypatch.setattr(
        profile_service_module, "ProfileService", lambda: ExternalSourceReturning(LibraryCollection(source="nuvio"))
    )
    settings = get_default_settings()

    assert asyncio.run(context.fetch_library_for_source("nuvio", settings, "tok", None, None)) is None


def test_ratings_survive_the_library_cache_round_trip(monkeypatch):
    fake = FakeMDBList({"tt1": rated("tt1", 9.5)})
    monkeypatch.setattr(ratings_module, "mdblist_service", fake)
    history = WatchHistory(items=[nuvio_item("tt1")], source="nuvio")
    library = watch_history_to_library_collection(asyncio.run(add_mdblist_ratings(history, KEY)))

    restored = LibraryCollection.model_validate_json(library.model_dump_json(by_alias=True))

    assert restored.loved[0].rating == 9.5 and restored.loved[0].rated_at == RATED_AT
