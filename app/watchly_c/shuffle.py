import random
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.settings import UserSettings
from app.services.catalog_definitions import get_config_id


def shuffle_period() -> str:
    """The current shuffle window, as a string that changes every SHUFFLE_INTERVAL_HOURS."""
    local = datetime.now(timezone.utc) + timedelta(hours=settings.DAILY_UTC_OFFSET_HOURS)
    return f"{local.date().isoformat()}:{local.hour // settings.SHUFFLE_INTERVAL_HOURS}"


def timed_shuffle(user_settings: UserSettings, catalog_id: str, data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shuffle a row once per window instead of on every request.

    A per-request shuffle reads as no shuffle at all: clients cache a row, and the
    order they keep is whichever one they fetched. A window-seeded order is the same
    on every request, so what the client shows changes when the window does. The
    shuffle runs on the cached row at serve time, so no rebuild is involved.

    Looked up by config id: served ids are slots (`watchly.item.2`, `watchly.theme.1`),
    which never matched a config entry, so those rows never shuffled.
    """
    config_id = get_config_id({"id": catalog_id})
    config = next((c for c in user_settings.catalogs if c.id == config_id), None)
    if config and config.shuffle:
        random.Random(f"{shuffle_period()}:{catalog_id}").shuffle(data)
    return data
