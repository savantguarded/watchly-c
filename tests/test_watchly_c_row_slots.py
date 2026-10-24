import asyncio

from app.core.settings import get_default_settings
from app.models.library import LibraryCollection
from app.services.catalog_definitions import DynamicCatalogService
from app.services.row_generator import RowDefinition
from app.watchly_c.row_slots import THEME_SLOTS, fill_slots


def theme_catalogs(monkeypatch, rows_by_type: dict[str, list[RowDefinition] | Exception]) -> tuple[list[dict], dict]:
    async def fake_rows(self, library, media_type, llm_config, token, user_settings=None):
        result = rows_by_type[media_type]
        if isinstance(result, Exception):
            raise result
        return media_type, result

    monkeypatch.setattr(DynamicCatalogService, "_build_theme_rows_for_type", fake_rows)
    slots: dict[str, dict[str, str]] = {}
    catalogs = asyncio.run(
        DynamicCatalogService()._build_theme_catalogs(
            LibraryCollection(), get_default_settings(), True, True, True, None, slots
        )
    )
    return catalogs, slots


def test_a_short_theme_build_still_declares_every_slot_in_order(monkeypatch):
    rows = {"movie": [RowDefinition(title="Heists", id="watchly.theme.g80")], "series": RuntimeError("profile")}
    catalogs, slots = theme_catalogs(monkeypatch, rows)

    assert [(c["type"], c["id"]) for c in catalogs] == [
        (t, f"watchly.theme.{n}") for t in ("movie", "series") for n in range(1, THEME_SLOTS + 1)
    ]
    assert catalogs[0]["name"] == "Heists"
    assert slots == {"movie": {"theme.1": "g80"}}


def test_a_switched_off_type_gets_no_slots(monkeypatch):
    monkeypatch.setattr(DynamicCatalogService, "_build_theme_rows_for_type", None)
    catalogs = asyncio.run(
        DynamicCatalogService()._build_theme_catalogs(
            LibraryCollection(), get_default_settings(), False, False, True, None, {}
        )
    )
    assert catalogs == []


def test_extra_built_rows_are_kept_past_the_slot_count():
    built = [{"type": "movie", "id": f"watchly.theme.{n}", "name": str(n), "extra": []} for n in range(1, 5)]

    filled = fill_slots(built, "watchly.theme.", {"movie": 3}, "x", [])

    assert [c["name"] for c in filled] == ["1", "2", "3", "4"]


def test_switching_it_off_restores_stock_slots(monkeypatch):
    monkeypatch.setattr("app.services.catalog_definitions.app_settings.FIXED_ROW_SLOTS", False)
    rows = {"movie": [RowDefinition(title="Heists", id="watchly.theme.g80")], "series": []}
    catalogs, _ = theme_catalogs(monkeypatch, rows)

    assert [c["id"] for c in catalogs] == ["watchly.theme.1"]
