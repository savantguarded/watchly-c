import json
import re
from typing import Any

import redis.asyncio as redis
from loguru import logger

from app.core.config import settings
from app.core.constants import CLAIMS_KEY
from app.core.settings import MAX_ITEM_ROWS
from app.services.redis_service import redis_service
from app.watchly_c.row_slots import CONTEXT_SLOTS, THEME_SLOTS

TYPES = ("movie", "series")
BASE_ROWS = ("watchly.rec", "watchly.creators", "watchly.all.loved", "watchly.liked.all")
# Earlier tiers keep a title when two rows want it: the rows built around what you
# watched, then the seasonal and contextual ones (narrow, so they'd starve behind the
# broad ones), then genre and keyword rows, then the rest. Within a tier, the lower slot.
SLOT_TIERS = {"item.mix": 0, "item": 0, "ctx": 1, "theme": 2}
SLOT_ID = re.compile(r"^watchly\.(item\.mix|item|ctx|theme)\.(\d+)$")

_ROW_IDS = (
    [f"watchly.item.mix.{n}" for n in range(1, MAX_ITEM_ROWS + 1)]
    + [f"watchly.item.{n}" for n in range(1, MAX_ITEM_ROWS + 1)]
    + [f"watchly.ctx.{n}" for n in range(1, CONTEXT_SLOTS + 1)]
    + [f"watchly.theme.{n}" for n in range(1, THEME_SLOTS + 1)]
    + list(BASE_ROWS)
)


def priority(catalog_id: str) -> tuple[int, int] | None:
    """None for rows that never take part: "Watch it again" is meant to repeat what was
    watched, and ids from manifests older than the slots carry no slot to order by."""
    slot = SLOT_ID.match(catalog_id)
    if slot:
        return SLOT_TIERS[slot[1]], int(slot[2])
    if catalog_id in BASE_ROWS:
        return len(SLOT_TIERS), BASE_ROWS.index(catalog_id)
    return None


def build_order(catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows that claim first are built first, so a refresh settles in one pass."""
    return sorted(catalogs, key=lambda c: priority(c["id"]) or (len(SLOT_TIERS) + 1, 0))


async def claimed_by_earlier_rows(token: str, catalog_id: str) -> dict[str, set[int]]:
    """TMDB ids, per type, that rows ahead of this one are showing.

    One MGET over a fixed set of keys. Each row writes only its own key, so building
    rows at the same time can't lose a claim, and nothing here is read on a cache hit.
    """
    claimed: dict[str, set[int]] = {t: set() for t in TYPES}
    mine = priority(catalog_id)
    if mine is None:
        return claimed

    keys = [
        CLAIMS_KEY.format(token=token, content_type=t, catalog_id=row)
        for t in TYPES
        for row in _ROW_IDS
        if (theirs := priority(row)) is not None and theirs < mine
    ]
    if not keys:
        return claimed
    try:
        client = await redis_service.get_client()
        stored = await client.mget(keys)
    except (redis.RedisError, OSError) as e:
        logger.warning(f"Row claims unavailable ({type(e).__name__}); {catalog_id} may repeat other rows' titles")
        return claimed

    for raw in filter(None, stored):
        for media_type, ids in json.loads(raw).items():
            claimed[media_type].update(ids)
    return claimed


async def record_claims(token: str, content_type: str, catalog_id: str, metas: list[dict[str, Any]]) -> None:
    """What this row is showing now, replacing what it showed before. A row with
    nothing to show drops its claim, so a slot that went empty doesn't hold titles back."""
    if priority(catalog_id) is None:
        return
    key = CLAIMS_KEY.format(token=token, content_type=content_type, catalog_id=catalog_id)
    ids: dict[str, list[int]] = {t: [] for t in TYPES}
    for meta in metas:
        if meta.get("_tmdb_id") and meta.get("type") in ids:
            ids[meta["type"]].append(meta["_tmdb_id"])
    if any(ids.values()):
        await redis_service.set(key, json.dumps(ids), settings.CATALOG_STALE_TTL)
    else:
        await redis_service.delete(key)
