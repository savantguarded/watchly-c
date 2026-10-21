from loguru import logger

from app.core.security import redact_token
from app.services.auth import auth_service
from app.services.context import extract_settings
from app.services.redis_service import redis_service
from app.services.stremio.service import StremioBundle
from app.services.token_store import token_store
from app.services.warmup import WARM_LOCK_KEY, warmup_service


async def warm_if_pending(token: str) -> None:
    """Run a saved account's warm-up inside this request.

    The configure page polls status one request at a time, so the poll that finds
    "pending" does the whole warm-up and the next one sees it finished. A lock held
    while the status still reads "pending" belongs to a warm-up that was frozen
    before its first step, so it is cleared rather than waited out.
    """
    if (await warmup_service.get_status(token)).get("state") != "pending":
        return
    credentials = await token_store.get_user_data(token)
    if not credentials:
        return

    bundle = StremioBundle()
    try:
        auth_key = await auth_service.resolve_auth_key_with_bundle(bundle, credentials, token)
    finally:
        await bundle.close()

    logger.info(f"[{redact_token(token)}] Running the warm-up inline")
    await redis_service.delete(WARM_LOCK_KEY.format(token=token))
    await warmup_service.prime(token, auth_key, extract_settings(credentials))
