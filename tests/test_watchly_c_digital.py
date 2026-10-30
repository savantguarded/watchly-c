import asyncio
from datetime import date

from app.services.recommendation import metadata
from app.services.recommendation.metadata import RecommendationMetadata
from app.watchly_c.digital import is_available

DAY = date(2026, 10, 30)


def movie(released: str | None, *dates: tuple[int, str]) -> dict:
    return {
        "id": 1,
        "title": "T",
        "release_date": released,
        "release_dates": {
            "results": [{"release_dates": [{"type": t, "release_date": f"{d}T00:00:00.000Z"} for t, d in dates]}]
        },
    }


def test_a_recent_movie_needs_a_digital_or_physical_release_on_or_before_today():
    assert not is_available(movie("2026-10-10", (3, "2026-10-10")), "movie", DAY)
    assert is_available(movie("2026-10-10", (3, "2026-10-10"), (4, "2026-10-29")), "movie", DAY)
    assert is_available(movie("2026-08-01", (5, "2026-10-30")), "movie", DAY)
    assert not is_available(movie("2026-10-10", (4, "2026-11-20")), "movie", DAY)


def test_an_older_movie_is_assumed_available_and_one_without_a_date_is_not():
    assert is_available(movie("1999-03-31"), "movie", DAY)
    assert not is_available(movie(""), "movie", DAY)


def test_a_series_must_have_started_airing():
    assert is_available({"first_air_date": "2026-10-30"}, "series", DAY)
    assert not is_available({"first_air_date": "2026-12-01"}, "series", DAY)
    assert not is_available({"first_air_date": ""}, "series", DAY)


class FakeTMDB:
    async def get_movie_details(self, tmdb_id: int):
        return {
            1: movie("2026-10-10", (3, "2026-10-10")),
            2: movie("2026-10-10", (4, "2026-10-25")),
        }[
            tmdb_id
        ] | {"id": tmdb_id, "external_ids": {"imdb_id": f"tt{tmdb_id}"}}

    async def get_images_for_title(self, media_type: str, tmdb_id: int, language: str):
        return {}


def batch(monkeypatch, **kwargs) -> list[str]:
    monkeypatch.setattr(metadata, "today", lambda: DAY)
    out = asyncio.run(RecommendationMetadata.fetch_batch(FakeTMDB(), [{"id": 1}, {"id": 2}], "movie", **kwargs))
    return [m["id"] for m in out]


def test_a_batch_leaves_out_movies_still_only_in_cinemas(monkeypatch):
    assert batch(monkeypatch) == ["tt2"]


def test_titles_the_user_already_watched_are_kept(monkeypatch):
    assert batch(monkeypatch, released_only=False) == ["tt1", "tt2"]


def test_the_switch_turns_the_filter_off(monkeypatch):
    monkeypatch.setattr(metadata.settings, "DIGITAL_RELEASE_ONLY", False)
    assert batch(monkeypatch) == ["tt1", "tt2"]
