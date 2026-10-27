from typing import Any

from app.core.constants import DISCOVER_ONLY_EXTRA
from app.core.settings import UserSettings

# Rows that are one profile-driven list per type in stock Watchly. Here each is a single row
# declared under one type, with movies and series alternating card by card.
MIXED_BASE_IDS = ("watchly.rec", "watchly.creators", "watchly.rewatch")


def mixed_base_catalog(user_settings: UserSettings, catalog_id: str, default_name: str) -> list[dict[str, Any]]:
    config = next((c for c in user_settings.catalogs if c.id == catalog_id), None)
    if not config or not config.enabled:
        return []
    if config.enabled_movie:
        home_type = "movie"
    elif config.enabled_series:
        home_type = "series"
    else:
        return []
    extra = [] if config.display_at_home else DISCOVER_ONLY_EXTRA
    return [{"type": home_type, "id": catalog_id, "name": config.name or default_name, "extra": extra}]
