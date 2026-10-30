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


def fits_theme(item: dict[str, Any], keywords: set[int], genres: set[int]) -> bool:
    """A row named for a keyword needs that keyword on the title; otherwise a genre of the row."""
    if keywords:
        return bool(keyword_ids(item) & keywords)
    return not genres or bool(set(item.get("genre_ids") or ()) & genres)


def theme_axis_ids(axes: dict[str, Any]) -> tuple[set[int], set[int]]:
    """Keyword and genre ids of a row's axes, as `fits_theme` takes them."""
    return tuple({int(v) for v in str(axes.get(name, "")).split("-") if v.isdigit()} for name in ("keyword", "genre"))
