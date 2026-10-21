import random
from typing import Any

from app.core.settings import UserSettings
from app.services.catalog_definitions import get_config_id
from app.watchly_c.seeds import today


def daily_shuffle(user_settings: UserSettings, catalog_id: str, data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shuffle a row once per day instead of on every request.

    A per-request shuffle reads as no shuffle at all: clients cache a row, and the
    order they keep is whichever one they fetched. A day-seeded order is the same on
    every request, so what the client shows changes exactly when the day does.

    Looked up by config id: served ids are slots (`watchly.item.2`, `watchly.theme.1`),
    which never matched a config entry, so those rows never shuffled.
    """
    config_id = get_config_id({"id": catalog_id})
    config = next((c for c in user_settings.catalogs if c.id == config_id), None)
    if config and config.shuffle:
        random.Random(f"{today().isoformat()}:{catalog_id}").shuffle(data)
    return data
