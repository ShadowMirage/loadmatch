from anthropic import AsyncAnthropic

from app.config import settings
from app.runtime import environment

_client = None


def _factory_is_mocked() -> bool:
    return type(AsyncAnthropic).__module__.startswith("unittest.mock")


def get_client():
    global _client

    if _client is not None:
        return _client

    if not environment.anthropic_enabled() and not _factory_is_mocked():
        return None

    _client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0, timeout=5.0)
    return _client


def client_is_mocked() -> bool:
    client = get_client()
    if client is None:
        return _factory_is_mocked()
    return type(client).__module__.startswith("unittest.mock")

