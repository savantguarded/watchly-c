import asyncio
import math
from typing import Any

from app.models.profile import TasteProfile
from app.services.recommendation.filtering import (
    RecommendationFiltering,
    filter_by_genres,
    filter_items_by_settings,
    filter_watched_by_imdb,
)
from app.services.recommendation.metadata import RecommendationMetadata
from app.services.recommendation.theme_based import ThemeBasedService
from app.services.recommendation.utils import content_type_to_mtype
from app.watchly_c.meta_keywords import is_meta_keyword
from app.watchly_c.relevance import BASE_FLOOR, clears_floor, quality

MANDATORY_AXES = ("country", "era")


class WideThemeService(ThemeBasedService):
    """Genre and keyword rows held to the rating floor and topped up to a full row.

    The stock row is one discover query, plus its parts when that comes up short, and
    stops there. A narrow combination (a genre, a keyword and a country) can run out
    well before 20, and a lenient popularity setting lets poorly rated titles in. This
    drops what is below the floor, then widens only as far as the row needs.
    """

    async def get_recommendations_for_theme(
        self,
        theme_id: str,
        content_type: str,
        profile: TasteProfile | None = None,
        watched_tmdb: set[int] | None = None,
        watched_imdb: set[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        picks = await super().get_recommendations_for_theme(
            theme_id, content_type, profile, watched_tmdb, watched_imdb, limit
        )
        picks = [p for p in picks if clears_floor(p, BASE_FLOOR)]
        if len(picks) >= limit:
            return picks
        return picks + await self._top_up(picks, theme_id, content_type, profile, watched_tmdb, watched_imdb, limit)

    async def _top_up(
        self,
        picks: list[dict[str, Any]],
        theme_id: str,
        content_type: str,
        profile: TasteProfile | None,
        watched_tmdb: set[int] | None,
        watched_imdb: set[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        anchors, flavors, fallbacks = self._parse_theme_id(theme_id)
        skip = (watched_tmdb or set()) | {p["_tmdb_id"] for p in picks}
        excluded = RecommendationFiltering.get_excluded_genre_ids(self.user_settings, content_type)
        need = limit - len(picks)

        found: dict[int, dict[str, Any]] = {}
        usable: list[dict[str, Any]] = []
        favourites = await self._favourite_keywords(profile)
        for params, pages in self._ladder(anchors, flavors, fallbacks, content_type, favourites):
            for item in await self._fetch_discover_candidates(content_type, params, pages):
                found.setdefault(item["id"], item)
            unique = filter_items_by_settings(list(found.values()), self.user_settings)
            usable = [i for i in filter_by_genres(unique, skip, excluded) if clears_floor(i, BASE_FLOOR)]
            # Twice what is needed: some are dropped once enriched.
            if len(usable) >= need * 2:
                break

        mtype = content_type_to_mtype(content_type)
        usable.sort(
            key=lambda i: (self._calculate_theme_score(i, anchors, flavors, fallbacks), quality(i, mtype)),
            reverse=True,
        )
        enriched = await RecommendationMetadata.fetch_batch(
            self.tmdb_service, usable[: math.ceil(need * 1.5)], content_type, user_settings=self.user_settings
        )
        return filter_watched_by_imdb(enriched, watched_imdb or set())[:need]

    async def _favourite_keywords(self, profile: TasteProfile | None) -> list[int]:
        """The profile's four best keywords that say something about a title. Names come
        from TMDB's cached keyword lookups, which the row generator has already made."""
        if not profile:
            return []
        ids = [k for k, _ in profile.get_top_keywords(limit=8)]
        details = await asyncio.gather(*(self.tmdb_service.get_keyword_details(k) for k in ids), return_exceptions=True)
        named = [
            k for k, d in zip(ids, details) if not isinstance(d, BaseException) and not is_meta_keyword(d.get("name"))
        ]
        return named[:4]

    def _ladder(
        self, anchors: dict, flavors: dict, fallbacks: dict, content_type: str, favourites: list[int]
    ) -> list[tuple[dict[str, Any], list[int]]]:
        """Queries tried in turn, each wider than the last: the same query deeper, the
        theme's keyword joined by the profile's best ones, the anchors alone, then the
        leading genre alone. Country and era stay on throughout; they define the row."""
        axes = {**anchors, **flavors, **fallbacks}
        kept = {name: axes[name] for name in MANDATORY_AXES if name in axes}
        min_rating, min_votes = RecommendationFiltering.get_quality_thresholds(self.user_settings)
        # Best rated first, but only among titles with enough votes to mean something.
        best = {
            "sort_by": "vote_average.desc",
            "vote_count.gte": max(300, min_votes),
            "vote_average.gte": max(BASE_FLOOR, min_rating),
        }

        def query(chosen: dict) -> dict[str, Any]:
            return {**best, **self._axes_to_params({**chosen, **kept}, content_type)}

        steps: list[tuple[dict[str, Any], list[int]]] = [(self._axes_to_params(axes, content_type), [4, 5, 6])]
        if "keyword" in axes and favourites:
            cluster = dict.fromkeys([*str(axes["keyword"]).split("-"), *map(str, favourites)])
            genre = {"genre": anchors["genre"]} if "genre" in anchors else {}
            steps.append((query({**genre, "keyword": "-".join(list(cluster)[:5])}), [1, 2, 3]))
        if anchors:
            steps.append((query(anchors), [1, 2, 3, 4]))
        if "genre" in axes:
            steps.append((query({"genre": str(axes["genre"]).split("-")[0]}), [1, 2, 3, 4, 5]))
        return steps
