import asyncio

import pytest

from app.core.settings import CatalogConfig, get_default_settings
from app.models.library import LibraryCollection, StremioLibraryItem
from app.services.catalog_definitions import DynamicCatalogService
from app.services.recommendation.catalog_service import catalog_service
from app.watchly_c import mixed_rows
from app.watchly_c.mixed_rows import MIXED_ITEM_PREFIX, interleave, mixed_item_rows, mixed_recommendations


def lib_item(item_id: str, mtype: str, loved: bool = False, mtime: str = "2026-10-01T00:00:00Z") -> StremioLibraryItem:
    return StremioLibraryItem(
        _id=item_id, type=mtype, name=f"Title {item_id}", temp=False, removed=False, _is_loved=loved, _mtime=mtime
    )


LIBRARY = LibraryCollection(
    loved=[lib_item("tt1", "movie", True), lib_item("tt2", "series", True)],
    watched=[lib_item("tt3", "movie"), lib_item("tt4", "series"), lib_item("tt5", "movie"), lib_item("tt6", "series")],
    source="nuvio",
)


def rows_for(config: CatalogConfig, library: LibraryCollection = LIBRARY) -> list[dict]:
    service = DynamicCatalogService()
    return asyncio.run(
        mixed_item_rows(library, config, None, service.build_catalog_entry, service._parse_item_last_watched)
    )


def test_rows_are_counted_in_total_not_per_type():
    rows = rows_for(CatalogConfig(id="watchly.item", rows=4))

    assert len(rows) == 4
    assert len({r["id"] for r in rows}) == 4
    assert all(r["id"].startswith(MIXED_ITEM_PREFIX) for r in rows)


def test_each_row_is_declared_under_its_seeds_type_and_labelled_from_it():
    types = {i.id: i.type for i in LIBRARY.all_items()}
    for row in rows_for(CatalogConfig(id="watchly.item", rows=4)):
        seed = row["id"].removeprefix(MIXED_ITEM_PREFIX)
        assert row["type"] == types[seed]
        expected = "Because you loved" if seed in {"tt1", "tt2"} else "Because you watched"
        assert row["name"] == f"{expected} Title {seed}"


def test_a_disabled_type_gives_no_seeds():
    rows = rows_for(CatalogConfig(id="watchly.item", rows=4, enabled_series=False))

    assert rows and all(r["type"] == "movie" for r in rows)


def test_a_disabled_catalog_gives_no_rows():
    assert rows_for(CatalogConfig(id="watchly.item", rows=4, enabled=False)) == []


def test_the_upstream_seed_pool_still_works_with_weighting_off(monkeypatch):
    monkeypatch.setattr(mixed_rows.settings, "WEIGHTED_ITEM_SEEDS", False)

    rows = rows_for(CatalogConfig(id="watchly.item", rows=4))

    assert len(rows) == 4


def meta(item_id: str, mtype: str) -> dict:
    return {"id": item_id, "type": mtype}


def test_every_third_card_is_the_other_type():
    same = [meta(f"m{n}", "movie") for n in range(10)]
    cross = [meta(f"s{n}", "series") for n in range(10)]

    out = interleave(same, cross, 9)

    assert [m["type"] for m in out] == ["movie", "movie", "series"] * 3


def test_a_short_side_is_topped_up_from_the_other():
    out = interleave([meta("m0", "movie")], [meta(f"s{n}", "series") for n in range(5)], 4)

    assert [m["id"] for m in out] == ["m0", "s0", "s1", "s2"]


class FakeItemService:
    def __init__(self, user_settings, tmdb_service, same):
        self.user_settings = user_settings
        self.tmdb_service = tmdb_service
        self.same = same
        self.calls = []

    async def get_recommendations_for_item(self, item_id, content_type, watched_tmdb, watched_imdb, limit):
        self.calls.append((item_id, content_type))
        return self.same


def discovered(tmdb_id: int) -> dict:
    return {"id": tmdb_id, "vote_average": 7.5, "vote_count": 900, "first_air_date": "2020-01-01", "genre_ids": []}


class FakeTMDB:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.discovers = []

    async def find_by_imdb_id(self, imdb_id):
        return 550, "movie"

    async def get_movie_details(self, tmdb_id):
        if self.fail:
            raise RuntimeError("tmdb down")
        return {"genres": [{"id": 28}, {"id": 18}, {"id": 27}], "keywords": {"keywords": [{"id": 9}, {"id": 10}]}}

    async def get_discover(self, media_type, with_genres=None, sort_by="popularity.desc", page=1, **kwargs):
        self.discovers.append((media_type, with_genres, kwargs.get("with_keywords")))
        if kwargs.get("with_keywords"):
            return {"results": [discovered(1), discovered(2), discovered(3)]}
        return {"results": [discovered(3), discovered(4)]}


async def fake_fetch_batch(tmdb_service, items, media_type, user_settings=None):
    return [{"id": f"tt{9000 + i['id']}", "type": "series" if media_type == "series" else "movie"} for i in items]


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(mixed_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)

    async def no_watched(library, content_type, user_settings):
        return set()

    monkeypatch.setattr(mixed_rows, "watched_tmdb_ids", no_watched)


def run_mixed(user_settings, tmdb, library=LIBRARY, limit=9):
    same = [meta(f"tt{100 + n}", "movie") for n in range(10)]
    service = FakeItemService(user_settings, tmdb, same)
    out = asyncio.run(mixed_recommendations(service, f"{MIXED_ITEM_PREFIX}tt1", "movie", set(), set(), library, limit))
    return out, service


def test_cross_type_picks_come_from_the_seeds_keywords_and_mapped_genres(patched):
    tmdb = FakeTMDB()
    out, service = run_mixed(get_default_settings(), tmdb)

    assert service.calls == [("tt1", "movie")]
    # Action maps to Action & Adventure; Horror has no series partner.
    assert ("series", "10759,18", None) in tmdb.discovers
    assert ("series", None, "9|10") in tmdb.discovers
    series = [m["id"] for m in out if m["type"] == "series"]
    # Matching on both keywords and genres leads.
    assert series[0] == "tt9003"
    assert [m["type"] for m in out] == ["movie", "movie", "series"] * 3


def test_watched_titles_of_the_other_type_are_left_out(patched):
    library = LibraryCollection(watched=[lib_item("tt9003", "series")], source="nuvio")
    out, _ = run_mixed(get_default_settings(), FakeTMDB(), library=library)

    assert "tt9003" not in {m["id"] for m in out}


def test_no_cross_picks_when_the_other_type_is_switched_off(patched):
    user_settings = get_default_settings()
    next(c for c in user_settings.catalogs if c.id == "watchly.item").enabled_series = False
    tmdb = FakeTMDB()

    out, _ = run_mixed(user_settings, tmdb)

    assert tmdb.discovers == []
    assert all(m["type"] == "movie" for m in out)


def test_a_cross_type_failure_still_serves_the_same_type_row(patched):
    out, _ = run_mixed(get_default_settings(), FakeTMDB(fail=True))

    assert len(out) == 9 and all(m["type"] == "movie" for m in out)


def test_mixed_ids_pass_validation_and_slot_resolution():
    catalog_id = f"{MIXED_ITEM_PREFIX}tt0468569"
    catalog_service._validate_inputs("tok", "series", catalog_id)

    assert asyncio.run(catalog_service._resolve_slot("tok", "series", catalog_id)) == catalog_id


def test_watched_titles_of_the_other_type_are_dropped_before_the_cut(patched, monkeypatch):
    async def watched(library, content_type, user_settings):
        return {3} if content_type == "series" else set()

    monkeypatch.setattr(mixed_rows, "watched_tmdb_ids", watched)
    out, _ = run_mixed(get_default_settings(), FakeTMDB())

    assert "tt9003" not in {m["id"] for m in out}
    assert [m["id"] for m in out if m["type"] == "series"][0] == "tt9001"
