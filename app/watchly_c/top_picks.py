from typing import Any

from app.services.profile.scorer import ProfileScorer
from app.watchly_c.relevance import clears_floor, quality

FLOOR = 6.8
MIN_VOTES = 200
# Taste decides what is considered, quality decides how far it climbs. At 0.4 a title
# the profile loves but the crowd rates 6.9 still loses to a close 8.0.
QUALITY_BASE = 0.4


def rank(items: list[dict[str, Any]], profile: Any, scorer: ProfileScorer, mtype: str) -> list[tuple[float, dict]]:
    """Profile fit scaled by quality, with no credit for being popular. Stock Top Picks
    pulls candidates in popularity order and adds a boost for the most popular ones."""
    scored = [
        (scorer.score_item(item, profile) * (QUALITY_BASE + (1 - QUALITY_BASE) * quality(item, mtype)), item)
        for item in items
        if clears_floor(item, FLOOR, MIN_VOTES)
    ]
    return sorted(scored, key=lambda s: s[0], reverse=True)
