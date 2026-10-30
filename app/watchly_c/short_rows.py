from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.core.constants import DEFAULT_CATALOG_LIMIT

# Rows whose own list is the point: topping them up with Top Picks would mislabel them.
NOT_TOPPED_UP = ("watchly.rec", "watchly.rewatch")
# A row that still falls short expires soon so it is retried, but not so soon that Stremio's
# burst of requests for every row rebuilds it each time.
SHORT_ROW_TTL_SECONDS = 600


async def top_up(
    row: list[dict[str, Any]],
    claimed: dict[str, set[int]],
    more: Callable[[dict[str, set[int]], int], Awaitable[list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Fills a row to a full row with `more(taken, limit)`, which must leave out `taken`: what
    other rows show plus what this row already holds."""
    missing = DEFAULT_CATALOG_LIMIT - len(row)
    if missing <= 0:
        return row
    taken = {content_type: set(ids) for content_type, ids in claimed.items()}
    for item in row:
        taken.setdefault(item["type"], set()).add(item["_tmdb_id"])
    try:
        extra = await more(taken, DEFAULT_CATALOG_LIMIT)
    except Exception as e:
        logger.warning(f"Topping up a row of {len(row)} failed: {type(e).__name__}")
        return row
    held = {item["id"] for item in row}
    return row + [item for item in extra if item["id"] not in held][:missing]
