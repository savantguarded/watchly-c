import asyncio

from app.core.settings import get_default_settings
from app.services.recommendation.theme_based import ThemeBasedService
from app.watchly_c import theme_rows
from app.watchly_c.theme_rows import WideThemeService

THEME = "watchly.theme.a:g27"
KEYWORD_THEME = "watchly.theme.a:g27.f:k5"


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
    return [
        {"id": f"tt{i['id']}", "_tmdb_id": i["id"], "type": "movie", "genre_ids": i.get("genre_ids", [])} for i in items
    ]


def serve(monkeypatch, stock: list[dict], tmdb: FakeTMDB, limit=20, theme=THEME):
    async def stock_row(self, *args, **kwargs):
        return stock

    monkeypatch.setattr(ThemeBasedService, "get_recommendations_for_theme", stock_row)
    monkeypatch.setattr(theme_rows.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    service = WideThemeService(tmdb, get_default_settings())
    return asyncio.run(service.get_recommendations_for_theme(theme, "movie", None, set(), set(), limit))


def stock_pick(tmdb_id: int, avg: float = 7.4) -> dict:
    return {
        "id": f"tt{tmdb_id}",
        "_tmdb_id": tmdb_id,
        "vote_average": avg,
        "vote_count": 900,
        "type": "movie",
        "genre_ids": [27],
    }


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


def ladder(anchors: dict, flavors: dict | None = None, fallbacks: dict | None = None) -> list[dict]:
    service = WideThemeService(FakeTMDB(depth=0), get_default_settings())
    return [params for params, _ in service._ladder(anchors, flavors or {}, fallbacks or {}, "movie")]


def test_a_keyword_row_keeps_its_keyword_in_every_widening_and_never_adds_others():
    steps = ladder({"keyword": "9715"}, {"genre": "28"}, {"country": "US"})

    assert len(steps) >= 2
    assert all(step["with_keywords"] == "9715" for step in steps)


def test_a_keyword_row_never_widens_to_its_genre_alone():
    steps = ladder({"genre": "28"}, {"keyword": "9715"})

    assert all(step.get("with_keywords") == "9715" for step in steps)


def test_a_genre_row_without_a_keyword_still_widens_to_its_leading_genre():
    steps = ladder({"genre": "27-53"})

    assert steps[-1]["with_genres"] == "27"
    assert all("with_keywords" not in step for step in steps)


def test_a_mixed_theme_row_alternates_its_own_type_with_the_others_matching_titles(monkeypatch):
    from app.watchly_c.theme_rows import mixed_theme_recommendations

    class BothTypes(FakeTMDB):
        async def get_discover(self, media_type, page=1, **kwargs):
            self.calls.append({"type": media_type, **kwargs})
            return {"results": [candidate(page * 1000 + n) for n in range(10)]}

    async def typed_batch(tmdb_service, items, media_type, user_settings=None):
        kind = "series" if media_type in ("tv", "series") else "movie"
        return [
            {"id": f"tt{kind}{i['id']}", "_tmdb_id": i["id"], "type": kind, "keywords": {"results": [{"id": 5}]}}
            for i in items
        ]

    async def native_row(self, theme_id, content_type, profile, watched_tmdb, watched_imdb, limit):
        return [
            {
                "id": f"ttnative{n}",
                "_tmdb_id": n,
                "type": content_type,
                "vote_average": 8,
                "vote_count": 999,
                "keywords": {"results": [{"id": 5}]},
            }
            for n in range(limit)
        ]

    monkeypatch.setattr(ThemeBasedService, "get_recommendations_for_theme", native_row)
    monkeypatch.setattr(theme_rows.RecommendationMetadata, "fetch_batch", typed_batch)
    tmdb = BothTypes(depth=0)
    service = WideThemeService(tmdb, get_default_settings())
    states = {t: (None, set(), set()) for t in ("movie", "series")}

    out = asyncio.run(
        mixed_theme_recommendations(
            service, "watchly.theme.series:a:g10765.f:k5", ["movie", "series"], states, {"movie": {1000}}, 20
        )
    )

    assert len(out) == 20
    assert [m["type"] for m in out[:4]] == ["series", "movie", "series", "movie"]
    movie_calls = [c for c in tmdb.calls if c["type"] == "movie"]
    assert movie_calls and all(c["with_genres"] == "878|14" for c in movie_calls)
    assert 1000 not in {m["_tmdb_id"] for m in out if m["type"] == "movie"}


def test_a_keyword_row_drops_picks_without_its_keyword(monkeypatch):
    stock = [
        {**stock_pick(1), "keywords": {"results": [{"id": 5}]}},
        {**stock_pick(2), "keywords": {"results": [{"id": 77}]}},
    ]

    out = serve(monkeypatch, stock, FakeTMDB(depth=0), limit=2, theme=KEYWORD_THEME)

    assert [p["_tmdb_id"] for p in out] == [1]
