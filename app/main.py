import contextlib
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
from app.models import (
    User, KycDocument, Truck, TruckSpaceListing,
    LoadRequest, Match, Conversation, OtpStore
)
from app.routers import webhook, admin, dashboard
from app.config import settings

logger = logging.getLogger(__name__)


class SecretFilter(logging.Filter):
    """Scrub known secrets from log records before they are emitted."""

    _SECRETS: list[tuple[str, str]] = []

    @classmethod
    def _build_secrets(cls) -> list[tuple[str, str]]:
        pairs = []
        for attr in ("WHATSAPP_TOKEN", "WA_TOKEN", "WHATSAPP_ACCESS_TOKEN",
                     "ANTHROPIC_API_KEY", "AWS_SECRET_KEY"):
            val = getattr(settings, attr, None)
            if val:
                pairs.append((val, "***"))
        return pairs

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not self._SECRETS:
            SecretFilter._SECRETS = self._build_secrets()
        msg = str(record.getMessage())
        for secret, replacement in self._SECRETS:
            if secret in msg:
                record.msg = record.msg.replace(secret, replacement)
                record.args = ()  # args already consumed, clear to avoid double-format
        return True


def _configure_logging() -> None:
    """Apply SecretFilter to the root logger so it covers the whole app."""
    root = logging.getLogger()
    secret_filter = SecretFilter()
    for handler in root.handlers:
        handler.addFilter(secret_filter)
    # In case handlers are added after this call (e.g. by uvicorn), also patch root
    root.addFilter(secret_filter)

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info("🚀 Starting loadmatch application. Database tables configured.")
    yield
    logger.info("Application winding down.")

app = FastAPI(title="loadmatch", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import webhook, admin, dashboard, debug
# ...
app.include_router(webhook.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(debug.router)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "loadmatch"}
