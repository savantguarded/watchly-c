from typing import Any

# RowGeneratorService plans up to three theme rows per type, by template or LLM.
THEME_SLOTS = 3


def fill_slots(
    catalogs: list[dict[str, Any]],
    prefix: str,
    slots_by_type: dict[str, int],
    name: str,
    extra: list[Any],
) -> list[dict[str, Any]]:
    """Every slot a row could fill, in slot order, whether today's build filled it or not.

    Nuvio saves the home order as a list of addon/type/catalog-id keys and appends any
    key it hasn't saved to the bottom. A slot that drops out when a build comes up a
    row short is an unsaved key again when it returns, so it lands at the bottom. An
    unfilled slot has no row-map definition and serves no metas, which Nuvio hides.
    """
    built = {(c["type"], c["id"]): c for c in catalogs}
    filled = []
    for media_type, count in slots_by_type.items():
        for slot in range(1, max(count, sum(t == media_type for t, _ in built)) + 1):
            catalog_id = f"{prefix}{slot}"
            filled.append(
                built.get((media_type, catalog_id))
                or {"type": media_type, "id": catalog_id, "name": name, "extra": extra}
            )
    return filled
