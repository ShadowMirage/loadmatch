import contextlib
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
from app.models import (
    User, KycDocument, Truck, TruckSpaceListing, 
    LoadRequest, Match, Conversation, OtpStore
)
from app.routers import webhook, admin

logger = logging.getLogger(__name__)

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting loadmatch application... Database tables are configured.")
    logger.info("Application starting up.")
    # Create all tables on startup (good for quick-start/dev if not using migrations)
    # Base.metadata.create_all(bind=engine)
    yield
    print("Application winding down.")

app = FastAPI(title="loadmatch", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router)
app.include_router(admin.router)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "loadmatch"}
