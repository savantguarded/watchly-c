import asyncio
import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.core.constants import SEED_LOG_KEY
from app.models.library import StremioLibraryItem, StremioState
from app.watchly_c import seeds as seeds_module
from app.watchly_c.seeds import choose_seeds, pick_seeds

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
TOKEN = "tok_seeds"


def item(
    item_id: str,
    days_ago: int,
    rating: float | None = None,
    rated_days_ago: int | None = None,
    times_watched: int = 1,
    completion: float = 1.0,
) -> StremioLibraryItem:
    return StremioLibraryItem(
        _id=item_id,
        type="movie",
        name=item_id,
        temp=False,
        removed=False,
        state=StremioState(
            lastWatched=NOW - timedelta(days=days_ago),
            duration=100,
            timeWatched=int(100 * completion),
            timesWatched=times_watched,
        ),
        _is_loved=(rating or 0) >= 9,
        _is_liked=7 <= (rating or 0) < 9,
        _rating=rating,
        _rated_at=NOW - timedelta(days=rated_days_ago) if rated_days_ago is not None else None,
    )


def last_watched(i: StremioLibraryItem) -> datetime:
    return i.state.lastWatched


def pick(items, k, avoid=frozenset(), seed=0):
    return pick_seeds(items, k, last_watched, set(avoid), random.Random(seed), NOW)


def test_rated_titles_beat_unrated_ones():
    items = [item(f"w{n}", 1) for n in range(10)] + [item(f"r{n}", 30, rating=8, rated_days_ago=30) for n in range(4)]

    assert {i.id for i in pick(items, 2)} <= {f"r{n}" for n in range(4)}


def test_a_low_rating_is_not_a_seed_while_better_ones_exist():
    items = [item("meh", 1, rating=5, rated_days_ago=1)] + [
        item(f"r{n}", 40, rating=9, rated_days_ago=40) for n in range(4)
    ]

    assert all(pick(items, 2, seed=s)[0].id != "meh" for s in range(30))


def test_recent_high_ratings_are_favoured_over_old_ones():
    recent = [item(f"new{n}", 200, rating=9.5, rated_days_ago=10) for n in range(3)]
    old = [item(f"old{n}", 160, rating=9.5, rated_days_ago=170) for n in range(3)]

    counts = Counter(pick(recent + old, 1, seed=s)[0].id[:3] for s in range(400))
    assert counts["new"] > counts["old"] * 2


def test_the_window_widens_when_recent_ratings_are_too_few():
    items = [item("recent", 5, rating=9, rated_days_ago=5)] + [
        item(f"older{n}", 300, rating=9, rated_days_ago=300) for n in range(5)
    ]

    picked = pick(items, 3)
    assert len(picked) == 3
    assert any(i.id.startswith("older") for i in picked)


def test_unrated_titles_top_up_a_short_rated_pool():
    items = [item("rated", 5, rating=9, rated_days_ago=5)] + [item(f"w{n}", 3) for n in range(5)]

    picked = pick(items, 3)
    assert picked[0].id == "rated"
    assert len({i.id for i in picked}) == 3


def test_without_ratings_recency_rewatches_and_completion_lead():
    strong = item("strong", 2, times_watched=3, completion=1.0)
    weak = item("weak", 170, completion=0.9)

    counts = Counter(pick([strong, weak], 1, seed=s)[0].id for s in range(400))
    assert counts["strong"] > counts["weak"] * 3


def test_recent_seeds_are_skipped_while_others_remain():
    items = [item(f"r{n}", 10, rating=9, rated_days_ago=10) for n in range(6)]

    picked = pick(items, 3, avoid={"r0", "r1", "r2"})
    assert {i.id for i in picked} == {"r3", "r4", "r5"}


def test_recent_seeds_return_when_nothing_else_is_left():
    items = [item(f"r{n}", 10, rating=9, rated_days_ago=10) for n in range(3)]

    assert len(pick(items, 3, avoid={"r0", "r1"})) == 3


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(seeds_module, "redis_service", fake)
    return fake


@pytest.fixture
def on_day(monkeypatch):
    def set_day(day: str):
        monkeypatch.setattr(seeds_module, "today", lambda: datetime.fromisoformat(day).date())

    return set_day


LIBRARY = [item(f"r{n}", 10, rating=9, rated_days_ago=10) for n in range(12)]


def choose(k=3, scope="movie", library=LIBRARY):
    return [i.id for i in asyncio.run(choose_seeds(TOKEN, library, k, scope, last_watched))]


def test_the_pick_is_stable_within_a_day(fake_redis, on_day):
    on_day("2026-10-21")
    first = choose()

    assert choose() == first


def test_two_builds_racing_on_a_fresh_day_agree(fake_redis, on_day):
    on_day("2026-10-21")
    first = choose()
    fake_redis.data.clear()

    assert choose() == first


def test_a_new_day_brings_seeds_not_used_this_week(fake_redis, on_day):
    used: list[str] = []
    for day in range(15, 19):
        on_day(f"2026-10-{day}")
        today = choose()
        assert not set(today) & set(used[-9:])
        used += today


def test_scopes_are_logged_separately(fake_redis, on_day):
    on_day("2026-10-21")
    choose(scope="movie")
    choose(scope="mixed")

    logged = json.loads(fake_redis.data[SEED_LOG_KEY.format(token=TOKEN)])
    assert set(logged["2026-10-21"]) == {"movie", "mixed"}


def test_old_days_are_dropped_from_the_log(fake_redis, on_day):
    on_day("2026-10-01")
    choose()
    on_day("2026-10-21")
    choose()

    assert list(json.loads(fake_redis.data[SEED_LOG_KEY.format(token=TOKEN)])) == ["2026-10-21"]


def test_a_title_gone_from_the_library_is_replaced(fake_redis, on_day):
    on_day("2026-10-21")
    first = choose()
    smaller = [i for i in LIBRARY if i.id != first[0]]

    again = choose(library=smaller)
    assert first[1:] == again[:2] and len(again) == 3 and first[0] not in again
