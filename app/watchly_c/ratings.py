import httpx
from loguru import logger

from app.models.history import WatchHistory, WatchHistoryItem
from app.services.mdblist import mdblist_service


async def add_mdblist_ratings(history: WatchHistory, api_key: str) -> WatchHistory:
    """Nuvio history with the user's MDBList ratings laid over it.

    Nuvio has no ratings, so without this every title is only "watched". Titles
    rated on MDBList but never played in Nuvio are added too: they were watched
    somewhere, and a rating is the strongest taste signal there is. The result
    stays a Nuvio history, so the cached library and profiles keep their source.

    When MDBList can't be reached the Nuvio history is returned as it is. That is
    a usable library, just without ratings, and the next daily refresh retries.
    """
    try:
        ratings = await mdblist_service.get_ratings(api_key)
    except httpx.HTTPStatusError as e:
        # The key is a query parameter: status code only, never the exception text.
        logger.warning(f"MDBList ratings unavailable (HTTP {e.response.status_code}); using Nuvio history alone")
        return history
    except Exception as e:
        logger.warning(f"MDBList ratings unavailable ({type(e).__name__}); using Nuvio history alone")
        return history

    items: list[WatchHistoryItem] = []
    for item in history.items:
        rated = ratings.pop(item.imdb_id, None)
        if rated is not None:
            item = item.model_copy(update={"rating": rated.rating, "rated_at": rated.rated_at})
        items.append(item)
    items.extend(rated.model_copy(update={"source": "nuvio"}) for rated in ratings.values())

    logger.info(f"Nuvio history: {len(history.items)} titles, {len(ratings)} more rated on MDBList only")
    return WatchHistory(items=items, source="nuvio")
