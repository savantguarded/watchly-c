from datetime import date
from typing import TypeVar

T = TypeVar("T")

# How far down the profile's ranked genres and keywords the rows reach. A day uses only its
# own slice of each pool, so a keyword comes back once every len(pool) / per-day days.
GENRE_POOL = 12
KEYWORD_POOL = 30


def day_slice(ranked: list[T], size: int, day: date) -> list[T]:
    """`size` entries of `ranked`, moved on by `size` every day and wrapping round, so each
    entry gets a day before any entry gets a second one. The rows are built from this slice
    alone: reaching past it is what brought the same keyword back most days."""
    if len(ranked) <= size:
        return ranked
    start = day.toordinal() * size % len(ranked)
    return [ranked[(start + i) % len(ranked)] for i in range(size)]
