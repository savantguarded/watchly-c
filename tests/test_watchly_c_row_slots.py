import asyncio

from app.core.settings import get_default_settings
from app.models.library import LibraryCollection
from app.services.catalog_definitions import DynamicCatalogService
from app.services.row_generator import RowDefinition
from app.watchly_c.row_slots import fill_slots


def theme_catalogs(monkeypatch, rows_by_type: dict[str, list[RowDefinition] | Exception]) -> tuple[list[dict], dict]:
    async def fake_rows(
        self,
        library,
        media_type,
        llm_config,
        token,
        user_settings=None,
        row_count=3,
        avoid_genres=None,
        avoid_keywords=None,
    ):
        result = rows_by_type[media_type]
        if isinstance(result, Exception):
            raise result
        return media_type, result

    monkeypatch.setattr(DynamicCatalogService, "_build_theme_rows_for_type", fake_rows)
    slots: dict[str, dict[str, str]] = {}
    catalogs = asyncio.run(
        DynamicCatalogService()._build_theme_catalogs(
            LibraryCollection(), get_default_settings(), True, True, True, None, slots, 3
        )
    )
    return catalogs, slots


def test_a_short_theme_build_still_declares_every_slot_in_order(monkeypatch):
    rows = {"movie": [RowDefinition(title="Heists", id="watchly.theme.g80")], "series": RuntimeError("profile")}
    catalogs, slots = theme_catalogs(monkeypatch, rows)

    assert [(c["type"], c["id"]) for c in catalogs] == [("movie", f"watchly.theme.{n}") for n in range(1, 4)]
    assert catalogs[0]["name"] == "Heists"
    assert slots == {"movie": {"theme.1": "movie:g80"}}


def test_a_switched_off_type_gets_no_slots(monkeypatch):
    monkeypatch.setattr(DynamicCatalogService, "_build_theme_rows_for_type", None)
    catalogs = asyncio.run(
        DynamicCatalogService()._build_theme_catalogs(
            LibraryCollection(), get_default_settings(), False, False, True, None, {}, 3
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


def test_rows_alternate_between_the_types_and_no_theme_repeats_across_them(monkeypatch):
    seen_avoid = {}
    rows = {
        "movie": [
            RowDefinition(title="Heists", id="watchly.theme.a:g80.f:k5"),
            RowDefinition(title="Mind Games", id="watchly.theme.a:g53"),
        ],
        "series": [RowDefinition(title="Space Sagas", id="watchly.theme.a:g10765")],
    }

    async def fake_rows(
        self,
        library,
        media_type,
        llm_config,
        token,
        user_settings=None,
        row_count=3,
        avoid_genres=None,
        avoid_keywords=None,
    ):
        seen_avoid[media_type] = (set(avoid_genres or ()), set(avoid_keywords or ()))
        return media_type, rows[media_type]

    monkeypatch.setattr(DynamicCatalogService, "_build_theme_rows_for_type", fake_rows)
    slots: dict[str, dict[str, str]] = {}
    catalogs = asyncio.run(
        DynamicCatalogService()._build_theme_catalogs(
            LibraryCollection(), get_default_settings(), True, True, True, None, slots, 3
        )
    )

    assert [c["name"] for c in catalogs] == ["Heists", "Space Sagas", "Mind Games"]
    assert {c["type"] for c in catalogs} == {"movie"}
    assert list(slots["movie"].values()) == ["movie:a:g80.f:k5", "series:a:g10765", "movie:a:g53"]
    assert seen_avoid["series"] == ({80}, {5})
