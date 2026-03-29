import logging

import redis

from app.config import settings
from app.runtime import environment

logger = logging.getLogger(__name__)

_client = None
_attempted = False


def get_client(force_refresh: bool = False):
    global _client, _attempted

    if force_refresh:
        _client = None
        _attempted = False

    if _client is not None:
        return _client

    if _attempted:
        return None

    _attempted = True

    if not environment.redis_enabled():
        return None

    try:
        candidate = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        candidate.ping()
        _client = candidate
        return _client
    except Exception as exc:
        logger.warning("Redis unavailable. Falling back to local/DB-safe runtime. Error=%s", exc)
        return None

