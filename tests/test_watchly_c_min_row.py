import asyncio

import pytest

from app.core.settings import get_default_settings
from app.services.recommendation import catalog_service as cs_module
from app.services.recommendation.catalog_service import catalog_service

TOKEN = "tok_min_row"


class Ctx:
    token = TOKEN
    user_settings = get_default_settings()
    library = bundle = auth_key = None


def metas(count: int) -> list[dict]:
    return [{"id": f"tt{n}", "type": "movie", "name": f"T{n}", "_tmdb_id": n} for n in range(count)]


@pytest.fixture
def build(monkeypatch):
    cached: dict[str, dict] = {}
    claims: dict[str, list] = {}
    rows: dict[str, list] = {}

    async def resolve(token, content_type, catalog_id):
        return catalog_id

    async def profile(token, content_type):
        return object(), set(), set()

    async def recommendations(**kwargs):
        return rows["recs"]

    async def set_catalog(token, content_type, catalog_id, catalog, ttl=None):
        cached[catalog_id] = catalog

    async def record(token, content_type, catalog_id, shown):
        claims[catalog_id] = shown

    async def no_claims(token, catalog_id):
        return {}

    monkeypatch.setattr(catalog_service, "_resolve_slot", resolve)
    monkeypatch.setattr(catalog_service, "_initialize_services", lambda user_settings: {"profile": None})
    monkeypatch.setattr(catalog_service, "_get_recommendations", recommendations)
    monkeypatch.setattr(cs_module.user_cache, "get_profile_and_watched_sets", profile)
    monkeypatch.setattr(cs_module.user_cache, "set_catalog", set_catalog)
    monkeypatch.setattr(cs_module, "record_claims", record)
    monkeypatch.setattr(cs_module, "claimed_by_earlier_rows", no_claims)

    def run(count: int):
        rows["recs"] = metas(count)
        data, _ = asyncio.run(catalog_service._build_catalog(Ctx(), "movie", "watchly.theme.1", {}))
        return data, cached, claims

    return run


def test_a_row_under_the_minimum_is_served_empty_and_claims_nothing(build):
    data, cached, claims = build(7)

    assert data["metas"] == []
    assert cached["watchly.theme.1"] == {"metas": []}
    assert claims["watchly.theme.1"] == []


def test_a_row_at_the_minimum_is_served(build):
    data, cached, claims = build(8)

    assert len(data["metas"]) == 8
    assert len(cached["watchly.theme.1"]["metas"]) == 8
    assert len(claims["watchly.theme.1"]) == 8


def test_an_empty_build_is_still_not_cached(build):
    data, cached, _ = build(0)

    assert data["metas"] == [] and cached == {}
