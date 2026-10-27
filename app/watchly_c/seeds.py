import json
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from loguru import logger

from app.core.config import settings
from app.core.constants import SEED_LOG_KEY
from app.core.security import redact_token
from app.models.library import StremioLibraryItem
from app.services.redis_service import redis_service

# Liked and loved. A seed is the title a whole row is built around, so a 6/10
# makes a poor one even when it was the latest thing rated.
SEED_RATING_FLOOR = 7.0
# Stremio thumbs carry no number; these stand in so they rank like ratings do.
LOVED_STAND_IN = 9.5
LIKED_STAND_IN = 8.0
# Widened in turn until the pool is big enough to pick from.
WINDOWS_DAYS: tuple[int | None, ...] = (182, 365, 730, None)
NO_REPEAT_DAYS = 7
SEED_LOG_TTL_SECONDS = (NO_REPEAT_DAYS + 2) * 86400


def today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=settings.DAILY_UTC_OFFSET_HOURS)).date()


def rating_of(item: StremioLibraryItem) -> float | None:
    if item.rating is not None:
        return item.rating
    if item.is_loved:
        return LOVED_STAND_IN
    if item.is_liked:
        return LIKED_STAND_IN
    return None


def _weight(item: StremioLibraryItem, last_active: datetime, now: datetime, window: int | None) -> float:
    """How strongly a title asks to be a seed: rating (or, unrated, how fully and how
    often it was watched) scaled by how recently the user was on it."""
    age_days = max((now - last_active).days, 0)
    recency = max(1 - age_days / window, 0) if window else 1 / (1 + age_days / 365)

    rating = rating_of(item)
    if rating is not None:
        strength = (rating - 6) / 4
    else:
        state = item.state
        completion = min(state.timeWatched / state.duration, 1.0) if state.duration else 1.0
        rewatches = min(max(state.timesWatched - 1, 0), 2)
        strength = (0.5 + 0.5 * completion) * (1 + 0.5 * rewatches)
    return strength * (0.25 + 0.75 * recency)


def pick_seeds(
    items: list[StremioLibraryItem],
    k: int,
    last_watched: Callable[[StremioLibraryItem], datetime],
    avoid: set[str],
    rng: random.Random,
    now: datetime,
) -> list[StremioLibraryItem]:
    """Weighted pick of k distinct seeds.

    Rated titles come first, from the narrowest recent window that still leaves a
    real choice; unrated titles only top up a pool that is still short. Seeds used
    in the last few days are skipped while there are enough others.
    """
    picked: list[StremioLibraryItem] = []
    rated = [i for i in items if (rating_of(i) or 0) >= SEED_RATING_FLOOR]
    unrated = [i for i in items if (rating_of(i) or 0) < SEED_RATING_FLOOR]

    for group in (rated, unrated):
        need = k - len(picked)
        if need <= 0:
            break
        active = {i.id: max(d for d in (i.rated_at, last_watched(i)) if d is not None) for i in group}
        for window in WINDOWS_DAYS:
            pool = [i for i in group if window is None or (now - active[i.id]).days <= window]
            if len(pool) >= 2 * need:
                break
        fresh = [i for i in pool if i.id not in avoid]
        for candidates in (fresh, [i for i in pool if i.id in avoid]):
            need = k - len(picked)
            if need <= 0:
                break
            # Efraimidis-Spirakis: a weighted sample without replacement in one sort.
            keyed = sorted(
                candidates,
                key=lambda i: rng.random() ** (1 / max(_weight(i, active[i.id], now, window), 1e-6)),
                reverse=True,
            )
            picked += keyed[:need]
    return picked


async def _read_log(token: str) -> dict[str, dict[str, list[str]]]:
    raw = await redis_service.get(SEED_LOG_KEY.format(token=token))
    return json.loads(raw) if raw else {}


async def reroll_seeds(token: str) -> None:
    """Drop today's picks so the next build draws new ones.

    Today's picks move into yesterday's entry: the draw is seeded by token and day, so
    only a changed avoid-set makes it land elsewhere, and filing them there also keeps
    them out of the next few days like any used seed.
    """
    log = await _read_log(token)
    day = today()
    todays = log.pop(day.isoformat(), None)
    if not todays:
        return
    yesterday = log.setdefault((day - timedelta(days=1)).isoformat(), {})
    for scope, ids in todays.items():
        yesterday[scope] = list(dict.fromkeys(yesterday.get(scope, []) + ids))
    await redis_service.set(SEED_LOG_KEY.format(token=token), json.dumps(log), SEED_LOG_TTL_SECONDS)


async def choose_seeds(
    token: str | None,
    items: list[StremioLibraryItem],
    k: int,
    scope: str,
    last_watched: Callable[[StremioLibraryItem], datetime],
) -> list[StremioLibraryItem]:
    """Today's seeds for one set of rows.

    Stable within a day: the first build of the day logs its pick and later
    rebuilds reuse it. The draw is also seeded by token and day, so two workers
    building at once land on the same pick.
    """
    day = today()
    by_id = {i.id: i for i in items}
    rng = random.Random(f"{token}:{day.isoformat()}:{scope}")
    now = datetime.now(timezone.utc)

    log = await _read_log(token) if token else {}

    kept = [by_id[i] for i in log.get(day.isoformat(), {}).get(scope, []) if i in by_id]
    if len(kept) >= k:
        return kept[:k]

    cutoff = (day - timedelta(days=NO_REPEAT_DAYS - 1)).isoformat()
    recent = {i for d, scopes in log.items() if cutoff <= d < day.isoformat() for i in scopes.get(scope, [])}
    kept_ids = {i.id for i in kept}
    rest = [i for i in items if i.id not in kept_ids]
    picked = kept + pick_seeds(rest, k - len(kept), last_watched, recent, rng, now)

    if token and picked:
        log = {d: scopes for d, scopes in log.items() if d >= cutoff}
        log.setdefault(day.isoformat(), {})[scope] = [i.id for i in picked]
        await redis_service.set(SEED_LOG_KEY.format(token=token), json.dumps(log), SEED_LOG_TTL_SECONDS)
        logger.debug(f"[{redact_token(token)}] {scope} seeds for {day}: {[i.id for i in picked]}")
    return picked
