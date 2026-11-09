import asyncio
from datetime import date

from app.core.settings import get_default_settings
from app.watchly_c import theme_rows
from app.watchly_c.theme_rows import ThemeAxes, WideThemeService, mixed_theme_recommendations

HORROR, THRILLER, CRIME, DRAMA = 27, 53, 80, 18
MURDER = 5


def candidate(tmdb_id: int, genres=(HORROR,), avg: float = 7.4, votes: int = 900, language="en") -> dict:
    return {
        "id": tmdb_id,
        "genre_ids": list(genres),
        "vote_average": avg,
        "vote_count": votes,
        "original_language": language,
        "release_date": "2015-01-01",
    }


class FakeTMDB:
    """Discover that honours the genre AND, the keyword and the runtime the way TMDB does,
    over a fixed catalogue."""

    def __init__(self, catalogue: list[dict], keyworded: set[int] | None = None, short: set[int] | None = None):
        self.catalogue = catalogue
        self.keyworded = keyworded or set()
        self.short = short
        self.calls: list[dict] = []

    async def get_discover(self, media_type, page=1, **kwargs):
        self.calls.append({"type": media_type, "page": page, **kwargs})
        items = self.catalogue
        if genres := kwargs.get("with_genres"):
            if "," in genres or "|" not in genres:
                need = {int(g) for g in genres.split(",")}
                items = [i for i in items if need <= set(i["genre_ids"])]
            else:
                any_of = {int(g) for g in genres.split("|")}
                items = [i for i in items if any_of & set(i["genre_ids"])]
        if kwargs.get("with_keywords"):
            items = [i for i in items if i["id"] in self.keyworded]
        if "with_runtime.lte" in kwargs and self.short is not None:
            items = [i for i in items if i["id"] in self.short]
        return {"results": items[(page - 1) * 20 : page * 20]}


def keyworded_batch(keyworded: set[int]):
    async def fetch_batch(tmdb_service, items, media_type, user_settings=None):
        kind = "series" if media_type in ("tv", "series") else "movie"
        return [
            {
                "id": f"tt{kind}{i['id']}",
                "_tmdb_id": i["id"],
                "type": kind,
                "genre_ids": i["genre_ids"],
                "keywords": {"keywords": [{"id": MURDER}] if i["id"] in keyworded else []},
            }
            for i in items
        ]

    return fetch_batch


def serve(monkeypatch, tmdb: FakeTMDB, theme: str, limit=20, day=date(2026, 11, 9)):
    monkeypatch.setattr(theme_rows.RecommendationMetadata, "fetch_batch", keyworded_batch(tmdb.keyworded))
    monkeypatch.setattr(theme_rows, "today", lambda: day)
    service = WideThemeService(tmdb, get_default_settings())
    return asyncio.run(service.get_recommendations_for_theme(theme, "movie", None, set(), set(), limit))


def test_a_two_genre_row_keeps_both_genres():
    axes = ThemeAxes.parse("watchly.theme.a:g18.f:g80")

    assert axes.genre_groups == (frozenset({DRAMA}), frozenset({CRIME}))


def test_a_crime_drama_row_holds_no_drama_without_crime(monkeypatch):
    catalogue = [candidate(n, genres=(DRAMA, CRIME)) for n in range(1, 6)] + [
        candidate(n, genres=(DRAMA,)) for n in range(100, 140)
    ]
    tmdb = FakeTMDB(catalogue)

    out = serve(monkeypatch, tmdb, "watchly.theme.a:g18.f:g80")

    assert sorted(m["_tmdb_id"] for m in out) == [1, 2, 3, 4, 5]
    assert all(call["with_genres"] == "18,80" for call in tmdb.calls)


def test_a_keyword_row_that_runs_out_stays_short_and_every_query_keeps_the_keyword(monkeypatch):
    catalogue = [candidate(n, genres=(CRIME,)) for n in range(1, 60)]
    tmdb = FakeTMDB(catalogue, keyworded={1, 2, 3})

    out = serve(monkeypatch, tmdb, f"watchly.theme.a:g80.f:k{MURDER}")

    assert sorted(m["_tmdb_id"] for m in out) == [1, 2, 3]
    assert all(call["with_keywords"] == str(MURDER) for call in tmdb.calls)


def test_poorly_rated_and_thinly_voted_titles_are_left_out(monkeypatch):
    catalogue = [candidate(1), candidate(2, avg=5.8), candidate(3, avg=9.4, votes=20)]

    out = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27")

    assert [m["_tmdb_id"] for m in out] == [1]


def test_subtitled_titles_only_come_up_in_a_country_row(monkeypatch):
    catalogue = [candidate(1), candidate(2, language="ja")]

    plain = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27")
    country = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27.f:ctJP")

    assert [m["_tmdb_id"] for m in plain] == [1]
    assert sorted(m["_tmdb_id"] for m in country) == [1, 2]


def test_only_the_runtime_is_relaxed_when_a_row_is_short(monkeypatch):
    catalogue = [candidate(n) for n in range(1, 41)]
    tmdb = FakeTMDB(catalogue, short={1, 2})

    out = serve(monkeypatch, tmdb, "watchly.theme.a:g27.b:rshort", limit=10)

    assert len(out) == 10
    assert any("with_runtime.lte" in c for c in tmdb.calls) and any("with_runtime.lte" not in c for c in tmdb.calls)
    assert all(c["with_genres"] == "27" for c in tmdb.calls)


def test_a_theme_serves_the_same_titles_all_day_and_different_ones_another_day(monkeypatch):
    catalogue = [candidate(n, avg=7.0 + (n % 10) / 10) for n in range(1, 61)]

    monday = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27", limit=10)
    again = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27", limit=10)
    tuesday = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27", limit=10, day=date(2026, 11, 10))

    assert [m["id"] for m in monday] == [m["id"] for m in again]
    assert {m["id"] for m in monday} != {m["id"] for m in tuesday}


def test_better_rated_titles_still_lead_more_often_than_not(monkeypatch):
    catalogue = [candidate(n, avg=8.4) for n in range(1, 31)] + [candidate(n, avg=6.8) for n in range(100, 130)]
    top_half = 0
    for day in range(1, 15):
        out = serve(monkeypatch, FakeTMDB(catalogue), "watchly.theme.a:g27", limit=10, day=date(2026, 11, day))
        top_half += sum(m["_tmdb_id"] < 100 for m in out)

    assert top_half > 14 * 10 * 0.7


def test_a_keyword_row_drops_picks_whose_details_lack_the_keyword(monkeypatch):
    catalogue = [candidate(1, genres=(CRIME,)), candidate(2, genres=(CRIME,))]
    tmdb = FakeTMDB(catalogue, keyworded={1, 2})
    monkeypatch.setattr(theme_rows.RecommendationMetadata, "fetch_batch", keyworded_batch({1}))
    monkeypatch.setattr(theme_rows, "today", lambda: date(2026, 11, 9))
    service = WideThemeService(tmdb, get_default_settings())

    out = asyncio.run(service.get_recommendations_for_theme(f"watchly.theme.a:g80.f:k{MURDER}", "movie", limit=2))

    assert [p["_tmdb_id"] for p in out] == [1]


def test_a_mixed_theme_row_alternates_its_own_type_with_the_others_matching_titles(monkeypatch):
    sci_fi_tv, sci_fi, fantasy = 10765, 878, 14
    catalogue = [candidate(n, genres=(sci_fi_tv,)) for n in range(1, 21)] + [
        candidate(n, genres=(sci_fi,) if n % 2 else (fantasy,)) for n in range(1000, 1020)
    ]
    tmdb = FakeTMDB(catalogue, keyworded=set(range(1, 21)) | set(range(1000, 1020)))
    monkeypatch.setattr(theme_rows.RecommendationMetadata, "fetch_batch", keyworded_batch(tmdb.keyworded))
    service = WideThemeService(tmdb, get_default_settings())
    states = {t: (None, set(), set()) for t in ("movie", "series")}

    out = asyncio.run(
        mixed_theme_recommendations(
            service, f"watchly.theme.series:a:g10765.f:k{MURDER}", ["movie", "series"], states, {"movie": {1000}}, 20
        )
    )

    assert len(out) == 20
    assert [m["type"] for m in out[:4]] == ["series", "movie", "series", "movie"]
    movie_calls = [c for c in tmdb.calls if c["type"] == "movie"]
    assert movie_calls and all(c["with_genres"] == "14|878" for c in movie_calls)
    assert 1000 not in {m["_tmdb_id"] for m in out if m["type"] == "movie"}
