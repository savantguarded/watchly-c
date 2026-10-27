import asyncio

from app.core.settings import get_default_settings
from app.services.recommendation.theme_based import ThemeBasedService
from app.watchly_c import theme_rows
from app.watchly_c.theme_rows import WideThemeService

THEME = "watchly.theme.a:g27"


def candidate(tmdb_id: int, avg: float = 7.4, votes: int = 900) -> dict:
    return {
        "id": tmdb_id,
        "genre_ids": [27],
        "vote_average": avg,
        "vote_count": votes,
        "original_language": "en",
        "release_date": "2015-01-01",
    }


class FakeTMDB:
    def __init__(self, depth: int):
        self.depth = depth
        self.calls: list[dict] = []

    async def get_discover(self, media_type, page=1, **kwargs):
        self.calls.append(kwargs)
        # The strict first query is thin; best-rated-first widening finds more.
        if kwargs.get("sort_by") != "vote_average.desc":
            return {"results": []}
        return {"results": [candidate(page * 1000 + n) for n in range(self.depth)]}


async def fake_fetch_batch(tmdb_service, items, media_type, user_settings=None):
    return [{"id": f"tt{i['id']}", "_tmdb_id": i["id"], "type": "movie"} for i in items]


def serve(monkeypatch, stock: list[dict], tmdb: FakeTMDB, limit=20):
    async def stock_row(self, *args, **kwargs):
        return stock

    monkeypatch.setattr(ThemeBasedService, "get_recommendations_for_theme", stock_row)
    monkeypatch.setattr(theme_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    service = WideThemeService(tmdb, get_default_settings())
    return asyncio.run(service.get_recommendations_for_theme(THEME, "movie", None, set(), set(), limit))


def stock_pick(tmdb_id: int, avg: float = 7.4) -> dict:
    return {"id": f"tt{tmdb_id}", "_tmdb_id": tmdb_id, "vote_average": avg, "vote_count": 900, "type": "movie"}


def test_a_short_row_is_topped_up_to_the_limit(monkeypatch):
    out = serve(monkeypatch, [stock_pick(1), stock_pick(2)], FakeTMDB(depth=20))

    assert len(out) == 20
    assert len({m["_tmdb_id"] for m in out}) == 20


def test_poorly_rated_stock_picks_are_dropped(monkeypatch):
    out = serve(monkeypatch, [stock_pick(1, avg=5.8), stock_pick(2)], FakeTMDB(depth=0), limit=5)

    assert [m["_tmdb_id"] for m in out] == [2]


def test_a_full_row_makes_no_extra_calls(monkeypatch):
    tmdb = FakeTMDB(depth=20)

    out = serve(monkeypatch, [stock_pick(n) for n in range(1, 21)], tmdb)

    assert len(out) == 20
    assert tmdb.calls == []


def test_top_up_never_repeats_a_stock_pick(monkeypatch):
    tmdb = FakeTMDB(depth=20)
    tmdb.depth = 3
    out = serve(monkeypatch, [stock_pick(1000)], tmdb, limit=5)

    assert [m["_tmdb_id"] for m in out].count(1000) == 1


def test_widening_keywords_skip_credit_tags(monkeypatch):
    from app.models.profile import TasteProfile

    class Names(FakeTMDB):
        async def get_keyword_details(self, keyword_id):
            return {"name": {1: "duringcreditsstinger", 2: "heist"}.get(keyword_id, "other")}

    service = WideThemeService(Names(depth=0), get_default_settings())
    profile = TasteProfile(keyword_scores={1: 9.0, 2: 8.0, 3: 7.0})

    assert asyncio.run(service._favourite_keywords(profile)) == [2, 3]
