import asyncio
import random

import pytest
from pydantic import ValidationError

from app.core.settings import ROW_LIMITS, CatalogConfig, get_default_settings
from app.models.profile import TasteProfile
from app.services.profile.vectorizer import ProfileVectorizer
from app.services.row_generator import RowGeneratorService, build_fallback_rows
from app.watchly_c.meta_keywords import is_meta_keyword

KEYWORDS = {n: f"theme {n}" for n in range(1, 13)}
PROFILE = TasteProfile(
    genre_scores={28: 9.0, 53: 8.0, 18: 7.0, 35: 6.0, 878: 5.0, 80: 4.0, 12: 3.0},
    keyword_scores={n: 20.0 - n for n in KEYWORDS},
    country_scores={"US": 5.0, "GB": 3.0},
)


def fallback(count: int) -> list:
    random.seed(count)
    genres = PROFILE.get_top_genres(limit=8)
    keywords = PROFILE.get_top_keywords(limit=12)
    return build_fallback_rows(genres, keywords, PROFILE.get_top_countries(limit=2), [], KEYWORDS, "movie", count)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 6])
def test_the_fallback_builds_as_many_distinct_rows_as_asked(count):
    rows = fallback(count)

    assert len(rows) == count
    assert len({title for _, title in rows}) == len(rows)


def test_stinger_and_credits_tags_are_not_themes():
    assert is_meta_keyword("duringcreditsstinger") and is_meta_keyword(" AfterCreditsStinger ")
    assert not is_meta_keyword("heist") and not is_meta_keyword(None)


def test_a_stinger_keyword_never_becomes_a_row(monkeypatch):
    service = RowGeneratorService.__new__(RowGeneratorService)
    names = {**KEYWORDS, 1: "duringcreditsstinger", 2: "aftercreditsstinger"}

    async def resolve(self, ids):
        return {k: names[k] for k in ids}

    monkeypatch.setattr(RowGeneratorService, "_resolve_keyword_names", resolve)
    for seed in range(30):
        random.seed(seed)
        rows = asyncio.run(service.generate_rows(PROFILE, "movie", llm_config=None, count=6))
        assert not any("stinger" in r.title.lower() for r in rows)
        assert not any("k1" in r.id.split(".") or "k2" in r.id for r in rows)


def test_new_profiles_leave_stinger_tags_out():
    metadata = {
        "genres": [{"id": 28}],
        "keywords": {"keywords": [{"id": 1, "name": "duringcreditsstinger"}, {"id": 2, "name": "heist"}]},
    }

    features = ProfileVectorizer.vectorize_item(metadata)

    assert features["keywords"] == [2]


def test_row_counts_are_bounded_per_catalog():
    CatalogConfig(id="watchly.theme", rows=ROW_LIMITS["watchly.theme"])
    CatalogConfig(id="watchly.ctx", rows=ROW_LIMITS["watchly.ctx"])
    with pytest.raises(ValidationError):
        CatalogConfig(id="watchly.theme", rows=ROW_LIMITS["watchly.theme"] + 1)
    with pytest.raises(ValidationError):
        CatalogConfig(id="watchly.item", rows=ROW_LIMITS["watchly.item"] + 1)


def test_based_on_all_loved_and_liked_are_retired_from_the_selector():
    ids = {c.id for c in get_default_settings().catalogs}

    assert not ids & {"watchly.all.loved", "watchly.liked.all"}
    assert {"watchly.rec", "watchly.theme", "watchly.ctx", "watchly.item"} <= ids


def test_a_theme_or_seasonal_config_saved_before_counts_keeps_its_three_rows():
    assert CatalogConfig(id="watchly.theme", rows=1).rows == 3
    assert CatalogConfig.model_validate({"id": "watchly.ctx"}).rows == 3
    assert CatalogConfig(id="watchly.theme", rows=2).rows == 2
    assert CatalogConfig(id="watchly.item", rows=1).rows == 1
