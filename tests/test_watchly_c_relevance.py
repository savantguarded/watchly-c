import asyncio

from app.core.settings import get_default_settings
from app.watchly_c import relevance
from app.watchly_c.relevance import (
    BASE_FLOOR,
    STRONG_SEED_FLOOR,
    RankedItemService,
    Seed,
    clears_floor,
    has_off_topic,
    on_topic,
    rank,
    rating_floor,
    row_size,
)

SEED = Seed(genres=(53, 18), language="en", year=2015, keywords=(1, 2))


def title(tmdb_id: int, genres=(53, 18), avg=7.5, votes=1000, **extra) -> dict:
    return {
        "id": tmdb_id,
        "genre_ids": list(genres),
        "vote_average": avg,
        "vote_count": votes,
        "original_language": "en",
        "release_date": "2015-05-01",
        **extra,
    }


def test_a_strong_seed_raises_the_floor():
    assert rating_floor(None) == BASE_FLOOR
    assert rating_floor(7.5) == BASE_FLOOR
    assert rating_floor(8.0) == STRONG_SEED_FLOOR


def test_thin_samples_and_low_averages_miss_the_floor():
    assert clears_floor(title(1, avg=7.0, votes=500), 6.5)
    assert not clears_floor(title(2, avg=6.2, votes=5000), 6.5)
    assert not clears_floor(title(3, avg=9.5, votes=12), 6.5)
    assert not clears_floor(title(4, avg=6.6, votes=120), 7.0)


def test_audience_and_format_genres_are_off_topic_unless_the_seed_has_them():
    assert has_off_topic([53, 16], [53])
    assert not has_off_topic([53, 16], [16])
    assert not on_topic([16, 35], [53, 18])
    assert not on_topic([35], [53, 18])
    assert on_topic([], [53, 18])


def test_a_close_match_outranks_a_loose_one_with_a_better_rating():
    close = title(1, avg=7.2, _rec_rank=0, _in_similar=True, _kw=2)
    loose = title(2, genres=(18, 35), avg=8.4, _rec_rank=50)

    ranked = rank([loose, close], SEED, "movie", BASE_FLOOR)

    assert [i["id"] for i in ranked] == [1, 2]


def test_rank_drops_poor_ratings_and_off_topic_titles():
    items = [title(1), title(2, avg=5.9), title(3, genres=(53, 16)), title(4, genres=(35,))]

    assert [i["id"] for i in rank(items, SEED, "movie", BASE_FLOOR)] == [1]


def test_foreign_language_titles_are_left_out_unless_the_seed_is_in_that_language():
    korean = title(1, original_language="ko") | {"original_language": "ko"}
    english = title(2)

    assert [i["id"] for i in rank([korean, english], SEED, "movie", BASE_FLOOR)] == [2]
    korean_seed = Seed(genres=(53, 18), language="ko", year=2015, keywords=())
    assert {i["id"] for i in rank([korean, english], korean_seed, "movie", BASE_FLOOR)} == {1, 2}


def test_rows_are_asked_for_more_than_they_show():
    assert row_size(20) > 20


class FakeTMDB:
    def __init__(self, recs: list[dict], discover: list[dict]):
        self.recs = recs
        self.discover = discover
        self.discovers: list[dict] = []

    async def find_by_imdb_id(self, imdb_id):
        return 100, "movie"

    async def get_movie_details(self, tmdb_id):
        return {
            "genres": [{"id": 53}, {"id": 18}],
            "original_language": "en",
            "release_date": "2015-01-01",
            "keywords": {"keywords": [{"id": 1}]},
        }

    async def get_recommendations(self, tmdb_id, media_type, page=1):
        return {"results": self.recs if page == 1 else []}

    async def get_similar(self, tmdb_id, media_type, page=1):
        return {"results": []}

    async def get_discover(self, media_type, page=1, **kwargs):
        self.discovers.append(kwargs)
        if kwargs.get("sort_by") == "vote_average.desc":
            return {"results": self.discover if page == 1 else []}
        return {"results": []}


async def fake_fetch_batch(tmdb_service, items, media_type, user_settings=None):
    return [
        {"id": f"tt{i['id']}", "_tmdb_id": i["id"], "type": "movie", "genre_ids": i.get("genre_ids", [])} for i in items
    ]


def serve(monkeypatch, tmdb: FakeTMDB, seed_rating=None, limit=20):
    monkeypatch.setattr(relevance.RecommendationMetadata, "fetch_batch", fake_fetch_batch)
    service = RankedItemService(tmdb, get_default_settings())
    return asyncio.run(
        service.get_recommendations_for_item("tt1", "movie", set(), set(), limit=limit, seed_rating=seed_rating)
    )


def test_poor_titles_never_reach_the_row_and_the_seed_itself_is_skipped(monkeypatch):
    recs = [title(100), title(11), title(12, avg=5.5)]

    out = serve(monkeypatch, FakeTMDB(recs, []), limit=5)

    assert [m["_tmdb_id"] for m in out] == [11]


def test_a_short_row_is_widened_with_best_rated_discover_results(monkeypatch):
    tmdb = FakeTMDB([title(11)], [title(n) for n in range(200, 230)])

    out = serve(monkeypatch, tmdb, limit=20)

    assert len(out) >= 20
    assert any(call.get("with_genres") == "53,18" for call in tmdb.discovers)


def test_a_strong_seed_keeps_only_better_titles(monkeypatch):
    recs = [title(11, avg=6.8), title(12, avg=7.8)]

    out = serve(monkeypatch, FakeTMDB(recs, []), seed_rating=9.0, limit=5)

    assert [m["_tmdb_id"] for m in out] == [12]
