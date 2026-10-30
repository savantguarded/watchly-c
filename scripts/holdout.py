"""Hold-out check for "Because you watched" rows.

Hides a share of the titles rated 8+, builds a row from each of several remaining 8+
titles with the library minus the hidden ones, and reports how many hidden titles turn up
in those rows. Compare runs before and after a scoring change on the same file and seed.

    TMDB_API_KEY=... uv run python scripts/holdout.py ratings.json [--engine stock]

ratings.json is a list of {"imdb_id": "tt...", "type": "movie"|"series", "rating": 9.0}.
"""

import argparse
import asyncio
import json
import random
from collections import defaultdict
from typing import Any

from app.core.config import settings
from app.core.settings import get_default_settings
from app.services.recommendation.item_based import ItemBasedService
from app.services.tmdb.service import get_tmdb_service
from app.watchly_c.relevance import RankedItemService

LIKED = 8.0


def split(titles: list[dict[str, Any]], share: float, rng: random.Random) -> tuple[list[dict], list[dict]]:
    liked = sorted((t for t in titles if t["rating"] >= LIKED), key=lambda t: t["imdb_id"])
    hidden = rng.sample(liked, k=max(1, round(len(liked) * share)))
    hidden_ids = {t["imdb_id"] for t in hidden}
    return hidden, [t for t in liked if t["imdb_id"] not in hidden_ids]


def score(rows: list[list[str]], hidden: set[str]) -> dict[str, float]:
    """Share of hidden titles in at least one row, and how high they sit in the rows they appear in."""
    positions = defaultdict(list)
    for row in rows:
        for position, imdb_id in enumerate(row):
            if imdb_id in hidden:
                positions[imdb_id].append(position)
    found = len(positions)
    ranks = [min(p) + 1 for p in positions.values()]
    return {"hit_rate": found / len(hidden), "mean_best_rank": sum(ranks) / found if found else 0.0}


async def run(path: str, engine: str, seeds: int, share: float, rng_seed: int) -> dict[str, dict[str, float]]:
    titles = json.load(open(path))
    service_class = ItemBasedService if engine == "stock" else RankedItemService
    service = service_class(get_tmdb_service(), get_default_settings())
    results = {}
    for content_type in ("movie", "series"):
        typed = [t for t in titles if t["type"] == content_type]
        if not any(t["rating"] >= LIKED for t in typed):
            continue
        hidden, visible = split(typed, share, random.Random(rng_seed))
        watched = {t["imdb_id"] for t in typed} - {t["imdb_id"] for t in hidden}
        picked = random.Random(rng_seed).sample(visible, k=min(seeds, len(visible)))
        built = await asyncio.gather(
            *(
                service.get_recommendations_for_item(
                    t["imdb_id"], content_type, watched_imdb=watched, seed_rating=t["rating"]
                )
                for t in picked
            )
        )
        rows = [[m["id"] for m in row] for row in built]
        results[content_type] = {
            "hidden": len(hidden),
            "seeds": len(picked),
            **score(rows, {t["imdb_id"] for t in hidden}),
        }
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ratings")
    parser.add_argument("--engine", choices=("ranked", "stock"), default="ranked")
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--share", type=float, default=0.2)
    parser.add_argument("--rng", type=int, default=1)
    args = parser.parse_args()
    assert settings.TMDB_API_KEY, "TMDB_API_KEY is not set"
    print(json.dumps(asyncio.run(run(args.ratings, args.engine, args.seeds, args.share, args.rng)), indent=2))
