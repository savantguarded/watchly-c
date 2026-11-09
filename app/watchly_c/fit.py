from typing import Any


def keyword_ids(item: dict[str, Any]) -> set[int]:
    block = item.get("keywords") or {}
    return {k["id"] for k in (block.get("keywords") or block.get("results") or [])}


def fits_seed(item: dict[str, Any], seed_genres: set[int], seed_keywords: set[int]) -> bool:
    """Shares a keyword with the seed, or at least two genres (one, for a seed that has
    only one). The candidate query only needed one of eight keywords or one genre, which
    is how a crime thriller ends up in a row built on a giant-robot film."""
    if keyword_ids(item) & seed_keywords:
        return True
    shared = set(item.get("genre_ids") or ()) & seed_genres
    return len(shared) >= min(2, len(seed_genres))
