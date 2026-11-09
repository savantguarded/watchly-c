from typing import Any


def keyword_ids(item: dict[str, Any]) -> set[int]:
    block = item.get("keywords") or {}
    return {k["id"] for k in (block.get("keywords") or block.get("results") or [])}
