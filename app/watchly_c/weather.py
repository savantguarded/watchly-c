from datetime import date

import httpx
from loguru import logger

from app.core.base_client import BaseClient
from app.core.config import settings
from app.core.constants import WEATHER_KEY
from app.services.redis_service import redis_service

RAIN_MM = 2.0
RAIN_TTL_SECONDS = 36 * 3600


async def rains_today(day: date) -> bool:
    """Whether the day's forecast rain, from Open-Meteo (free, no key), at the configured
    coordinates. One request a day across every account: the answer is kept in Redis,
    and a failed lookup is kept as "dry" so the accounts that follow don't each retry it."""
    key = WEATHER_KEY.format(day=day.isoformat())
    cached = await redis_service.get(key)
    if cached is not None:
        return cached == "1"

    client = BaseClient(base_url="https://api.open-meteo.com", timeout=5.0, max_retries=2)
    try:
        data = await client.get(
            "/v1/forecast",
            params={
                "latitude": settings.WEATHER_LATITUDE,
                "longitude": settings.WEATHER_LONGITUDE,
                "daily": "precipitation_sum",
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        rain = float(((data.get("daily") or {}).get("precipitation_sum") or [0])[0] or 0) >= RAIN_MM
    except httpx.HTTPError as e:
        logger.warning(f"Rain forecast unavailable ({type(e).__name__}); no Rainy Day row today")
        rain = False
    finally:
        await client.close()

    await redis_service.set(key, "1" if rain else "0", RAIN_TTL_SECONDS)
    return rain
