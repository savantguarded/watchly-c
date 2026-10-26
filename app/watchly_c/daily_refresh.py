import hmac
import time

from fastapi import APIRouter, Header, HTTPException
from loguru import logger

from app.core.config import settings
from app.core.constants import DAILY_REFRESH_RUN_KEY
from app.core.security import redact_token
from app.core.settings import UserSettings
from app.services.auth import auth_service
from app.services.catalog_updater import catalog_updater
from app.services.context import extract_settings, load_user_context
from app.services.manifest import manifest_service
from app.services.recommendation.catalog_service import catalog_service
from app.services.redis_service import redis_service
from app.services.stremio.service import StremioBundle
from app.services.token_store import token_store
from app.watchly_c.seeds import today

router = APIRouter(tags=["watchly-c"])

# Vercel stops a function at 300 s; no new account is started past this, so the one
# in flight has room to finish. Accounts left over refresh lazily on their next open.
TIME_BUDGET_SECONDS = 200
RUN_KEY_TTL_SECONDS = 20 * 3600


@router.get("/watchly-c/daily-refresh", summary="Rebuild every account's library, seeds and rows for the day")
async def daily_refresh(authorization: str | None = Header(default=None)) -> dict:
    """Called by the Vercel cron in vercel.json.

    Without CRON_SECRET anyone can call this, and the run key is what keeps that
    harmless: one run per day, whoever asks first.
    """
    if settings.CRON_SECRET and not hmac.compare_digest(authorization or "", f"Bearer {settings.CRON_SECRET}"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    day = today().isoformat()
    if not await redis_service.set_nx(DAILY_REFRESH_RUN_KEY.format(day=day), "1", RUN_KEY_TTL_SECONDS):
        return {"day": day, "status": "already-ran"}

    return {"day": day, "status": "done", **await refresh_all_accounts()}


async def _account_tokens() -> list[str]:
    # A SCAN, which is fine here: this runs once a day off the request path, over a
    # personal instance's handful of accounts.
    client = await redis_service.get_client()
    prefix = settings.REDIS_TOKEN_KEY
    return [key.removeprefix(prefix) async for key in client.scan_iter(match=f"{prefix}*", count=500)]


async def refresh_all_accounts() -> dict[str, int]:
    started = time.monotonic()
    tokens = await _account_tokens()
    refreshed = failed = deferred = 0

    for token in tokens:
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            deferred += 1
            continue
        credentials = await token_store.get_user_data(token)
        if not credentials:
            continue
        try:
            ok = await refresh_account(token, credentials)
        except Exception as e:
            logger.warning(f"[{redact_token(token)}] Daily refresh failed: {type(e).__name__}")
            ok = False
        refreshed += ok
        failed += not ok

    logger.info(f"Daily refresh: {refreshed} refreshed, {failed} failed, {deferred} left for later")
    return {"refreshed": refreshed, "failed": failed, "deferred": deferred}


async def refresh_account(token: str, credentials: dict) -> bool:
    """Refetch the library, then rebuild the manifest (today's seeds) and every row.

    The stock refresh refetches only Stremio libraries, so an external-source library
    otherwise stays whatever was cached first. The refetch here keeps the cached copy
    when the source fails or comes back empty.
    """
    user_settings = extract_settings(credentials)
    if user_settings.watch_history_source != "stremio":
        bundle = StremioBundle()
        try:
            auth_key = await auth_service.resolve_auth_key_with_bundle(bundle, credentials, token)
            await manifest_service.cache_library_and_profiles(bundle, auth_key, user_settings, token)
        finally:
            await bundle.close()

    refreshed = await catalog_updater.refresh_catalogs_for_credentials(token, credentials)
    if refreshed and settings.DAILY_REWATCH:
        await rebuild_rewatch(token, user_settings)
    return refreshed


async def rebuild_rewatch(token: str, user_settings: UserSettings) -> None:
    """The stock row keeps its cached set until it is a day old, then rebuilds behind
    the first request after that, so a day's set reached the screen a visit late. Built
    here, awaited, it is in the cache before anyone opens Nuvio."""
    config = next((c for c in user_settings.catalogs if c.id == "watchly.rewatch" and c.enabled), None)
    if config is None:
        return
    ctx = await load_user_context(token)
    try:
        for content_type, on in (("movie", config.enabled_movie), ("series", config.enabled_series)):
            if on:
                await catalog_service._build_catalog(ctx, content_type, "watchly.rewatch", catalog_service._headers())
    finally:
        await ctx.close()
