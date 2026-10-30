from datetime import date, timedelta
from typing import Any

# Older films often have no digital date on TMDB, so only recent ones are checked.
CHECKED_FOR_DAYS = 365
DIGITAL, PHYSICAL = 4, 5


def is_available(details: dict[str, Any], media_type: str, day: date) -> bool:
    """Whether a title can be watched at home: a movie needs a digital or physical release
    in any country on or before `day`, a series needs to have started airing."""
    if media_type != "movie":
        return bool(details.get("first_air_date")) and details["first_air_date"] <= day.isoformat()
    released = details.get("release_date")
    if not released:
        return False
    if date.fromisoformat(released) < day - timedelta(days=CHECKED_FOR_DAYS):
        return True
    return any(
        release["type"] in (DIGITAL, PHYSICAL) and release["release_date"][:10] <= day.isoformat()
        for country in details["release_dates"]["results"]
        for release in country["release_dates"]
    )
