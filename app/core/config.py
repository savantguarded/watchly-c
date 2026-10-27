from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.version import __version__


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",
    )

    # Debug is deliberately noisy — per-item and per-cache-hit detail — so it is
    # opt-in rather than the default the app used to run on.
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    TMDB_API_KEY: str | None = None
    PORT: int = 8000
    ADDON_ID: str = "com.bimal.watchly"
    ADDON_NAME: str = "Watchly"
    REDIS_URL: str = "redis://redis:6379/0"
    # Maximum number of connections Redis client will open per process
    # Set conservatively to avoid unbounded connection growth under high concurrency
    REDIS_MAX_CONNECTIONS: int = 20
    # If total connected clients reported by Redis exceeds this, background
    # Redis-heavy jobs will back off. Tune according to your Redis capacity.
    REDIS_CONNECTIONS_THRESHOLD: int = 100
    REDIS_TOKEN_KEY: str = "watchly:token:"
    TOKEN_SALT: str = "change-me"
    TOKEN_TTL_SECONDS: int = 0  # 0 = never expire
    ANNOUNCEMENT_HTML: str = ""
    ALLOW_SIGNUPS: bool = True
    AUTO_UPDATE_CATALOGS: bool = True
    CATALOG_REFRESH_INTERVAL_SECONDS: int = 86400  # 24 hours
    APP_ENV: Literal["development", "production", "vercel"] = "production"
    HOST_NAME: str = "https://1ccea4301587-watchly.baby-beamup.club"

    RECOMMENDATION_SOURCE_ITEMS_LIMIT: int = 10
    LIBRARY_ITEMS_LIMIT: int = 20

    # How long a client may reuse a catalog response. Short on purpose: served ids
    # are stable slots now, so this header is the only thing telling Stremio a row's
    # content has moved. stale-while-revalidate keeps the UI instant past it.
    CATALOG_CACHE_TTL: int = 60
    CATALOG_STALE_TTL: int = 604800  # 7 days (soft expiration fallback)

    # External history providers (OAuth app credentials)
    TRAKT_CLIENT_ID: str | None = None
    TRAKT_CLIENT_SECRET: str | None = None
    SIMKL_CLIENT_ID: str | None = None
    SIMKL_CLIENT_SECRET: str | None = None

    # Watchly C tweaks. Each one switches back to stock Watchly behaviour when off.
    NUVIO_MDBLIST_RATINGS: bool = True
    WEIGHTED_ITEM_SEEDS: bool = True
    MIXED_ITEM_ROWS: bool = True
    WATCHED_TMDB_IDS: bool = True
    FIXED_ROW_SLOTS: bool = True
    FILL_MISSING_TITLES: bool = True
    DAILY_REWATCH: bool = True
    REROLL_SEEDS_ON_REFRESH: bool = True
    RELEVANCE_RANKING: bool = True
    WIDE_THEME_ROWS: bool = True
    CONTEXT_ROWS: bool = True
    CROSS_ROW_DEDUPE: bool = True
    QUALITY_TOP_PICKS: bool = True
    RETIRE_ALL_BASED: bool = True
    MIXED_BASE_ROWS: bool = True
    # Rows with Shuffle on keep one order for this many hours, then reshuffle. 0 is
    # stock Watchly: a new order on every request.
    SHUFFLE_INTERVAL_HOURS: int = 1
    # Hours from UTC where "a day" starts for daily seeds and shuffles. An offset rather
    # than a zone name: zone data isn't guaranteed on every host, and Lagos has no DST.
    DAILY_UTC_OFFSET_HOURS: float = 1.0
    # Where "is it raining today" is looked up for the Rainy Day row. Lagos.
    WEATHER_LATITUDE: float = 6.5244
    WEATHER_LONGITUDE: float = 3.3792
    # Vercel sends this as a Bearer token on cron calls once it is set in the project.
    CRON_SECRET: str | None = None
    # Run the setup warm-up inside the configure page's status poll rather than behind
    # the save response. Vercel freezes a function once it has answered, so a warm-up
    # left running there never gets past "Saving your configuration". On by itself on
    # Vercel, which sets VERCEL=1 at runtime.
    INLINE_WARMUP: bool = Field(default=False, validation_alias=AliasChoices("INLINE_WARMUP", "VERCEL"))


settings = Settings()

APP_VERSION = __version__
