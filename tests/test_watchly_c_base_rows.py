import asyncio

from app.core.settings import CatalogConfig, get_default_settings
from app.models.library import LibraryCollection
from app.models.profile import TasteProfile
from app.services.recommendation.catalog_service import catalog_service
from app.watchly_c import mixed_context
from app.watchly_c.base_rows import mixed_base_catalog


def settings_with(**changes):
    user_settings = get_default_settings()
    config = next(c for c in user_settings.catalogs if c.id == "watchly.rec")
    for name, value in changes.items():
        setattr(config, name, value)
    return user_settings


def test_top_picks_is_one_row_under_one_type():
    rows = mixed_base_catalog(settings_with(), "watchly.rec", "Top Picks for You")

    assert [(r["type"], r["id"]) for r in rows] == [("movie", "watchly.rec")]


def test_a_series_only_setup_homes_the_row_under_series():
    rows = mixed_base_catalog(settings_with(enabled_movie=False), "watchly.rec", "Top Picks")

    assert [r["type"] for r in rows] == ["series"]


def test_disabled_or_typeless_rows_are_not_declared():
    assert mixed_base_catalog(settings_with(enabled=False), "watchly.rec", "x") == []
    assert mixed_base_catalog(settings_with(enabled_movie=False, enabled_series=False), "watchly.rec", "x") == []
    assert mixed_base_catalog(get_default_settings(), "watchly.creators", "x") == []


class FakeTopPicks:
    def __init__(self):
        self.calls = []

    async def get_top_picks(self, profile, content_type, library_items, watched_tmdb, watched_imdb, limit):
        self.calls.append((content_type, limit, set(watched_tmdb)))
        return [{"id": f"{content_type}{n}", "type": content_type, "_tmdb_id": n} for n in range(limit)]


def serve(monkeypatch, user_settings, claimed=None):
    async def other(token, library, content_type, settings_):
        return TasteProfile(), {7}, set()

    monkeypatch.setattr(mixed_context, "other_type_state", other)
    picks = FakeTopPicks()
    out = asyncio.run(
        catalog_service._get_recommendations(
            "watchly.rec",
            "movie",
            {"top_picks": picks},
            TasteProfile(),
            {5},
            set(),
            LibraryCollection(),
            20,
            user_settings,
            claimed,
            "tok",
        )
    )
    return out, picks


def test_top_picks_alternates_movies_and_series_in_one_row(monkeypatch):
    out, picks = serve(monkeypatch, get_default_settings())

    assert len(out) == 20
    assert [m["type"] for m in out[:6]] == ["movie", "series"] * 3
    assert sorted(c[0] for c in picks.calls) == ["movie", "series"]


def test_each_side_leaves_out_its_own_watched_and_claimed_titles(monkeypatch):
    _, picks = serve(monkeypatch, get_default_settings(), {"series": {9}})

    seen = {content_type: skip for content_type, _, skip in picks.calls}
    assert seen["movie"] == {5}
    assert seen["series"] == {7, 9}


def test_a_single_type_setup_makes_a_plain_row(monkeypatch):
    out, picks = serve(monkeypatch, settings_with(enabled_series=False))

    assert {m["type"] for m in out} == {"movie"} and len(out) == 20
    assert [c[0] for c in picks.calls] == ["movie"]


def test_the_movies_and_series_order_setting_is_hidden_when_every_row_is_one_type():
    from fastapi.testclient import TestClient

    from app.core.app import app

    html = TestClient(app).get("/configure").text

    assert "sortingOrderSelect" not in html
