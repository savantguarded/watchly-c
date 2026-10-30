from datetime import date
from typing import TypeVar

T = TypeVar("T")

# Genres a day's rows can anchor on, so a week of rows reaches past the profile's top three.
GENRE_POOL = 8


def rotated(ranked: list[T], window: int, day: date) -> list[T]:
    """The profile's ranked list turned by one window a day. Rows pick from the first
    `window` entries, so each day they draw on a different slice and every entry gets its turn."""
    if len(ranked) <= window:
        return ranked
    shift = day.toordinal() * window % len(ranked)
    return ranked[shift:] + ranked[:shift]
