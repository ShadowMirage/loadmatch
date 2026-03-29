import asyncio
import contextlib
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.core.trace_context import get_trace_id
from app.database import SessionLocal, engine
from app.runtime import environment
from app.runtime.redis_adapter import get_client as get_redis_client
from app.routers import admin, dashboard, debug, webhook
from app.services.event_bus import EventBus
from app.services.recovery_daemon import RecoveryDaemon

class TraceIdFilter(logging.Filter):
    def filter(self, record):
        try:
            record.trace_id = get_trace_id() or "system"
        except Exception:
            record.trace_id = "system"
        return True


def _install_trace_record_factory() -> None:
    """Ensure every LogRecord has a trace_id before any formatter runs."""
    current_factory = logging.getLogRecordFactory()

    if getattr(current_factory, "_loadmatch_trace_factory", False):
        return

    def record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        if not hasattr(record, "trace_id"):
            try:
                record.trace_id = get_trace_id() or "system"
            except Exception:
                record.trace_id = "system"
        return record

    record_factory._loadmatch_trace_factory = True
    logging.setLogRecordFactory(record_factory)


_install_trace_record_factory()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s trace=%(trace_id)s %(message)s"
)

# Trace ID and secret filters will be attached in _configure_logging

for name in (
    "gunicorn.error",
    "gunicorn.access",
    "uvicorn.error",
    "uvicorn.access",
):
    logging.getLogger(name).propagate = True

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
    """Apply SecretFilter and TraceIdFilter to the root logger so it covers the whole app."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    secret_filter = SecretFilter()
    trace_filter = TraceIdFilter()

    for handler in root.handlers:
        if not getattr(handler, "_loadmatch_filters_installed", False):
            handler.addFilter(secret_filter)
            handler.addFilter(trace_filter)
            handler._loadmatch_filters_installed = True

    if not getattr(root, "_loadmatch_root_filters_installed", False):
        root.addFilter(secret_filter)
        root.addFilter(trace_filter)
        root._loadmatch_root_filters_installed = True
  
  
  
  
  
  
LOCK_ID = 918273645
 
 
def _acquire_leader_lock(engine) -> Optional[Any]:
    """Uses a PostgreSQL advisory lock on a persistent connection for leader election."""
    try:
        # Check environment override first (as a kill-switch)
        if os.environ.get("IS_PRIMARY_RECOVERY_NODE", "0") != "1":
            return None
        
        # Persistent connection for the life of the lock
        conn = engine.connect()
        
        # Session-level advisory lock: pg_try_advisory_lock
        # Returns True if success, False otherwise.
        result = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}).scalar()
        
        if result:
            return conn
            
        conn.close()
        return None
    except Exception as e:
        logger.error(f"Failed to acquire leader lock: {e}")
        return None


async def _start_primary_services(app: FastAPI):
    """
    Atomic startup of primary services:
    - Instantiate RecoveryDaemon
    - Start Recovery Loop task
    - Perform initial startup zombie sweep
    - Start Leadership Heartbeat task
    - Cancel any existing watchdog task
    """
    # 1. Cancel standby watchdog
    watchdog_task = getattr(app.state, "watchdog_task", None)
    if watchdog_task and not watchdog_task.done():
        watchdog_task.cancel()
        app.state.watchdog_task = None
        logger.info("Leadership watchdog cancelled (Promotion Active)")

    # 2. Get DB PID for observability
    app.state.db_pid = app.state.leader_conn.execute(text("SELECT pg_backend_pid()")).scalar()
    logger.info(f"PRIMARY_NODE: RecoveryDaemon active (Leader Election Success, db_pid={app.state.db_pid})")

    # 3. Instantiate and start RecoveryDaemon
    app.state.recovery_daemon = RecoveryDaemon(SessionLocal)
    app.state.recovery_task = asyncio.create_task(app.state.recovery_daemon.run_forever())

    # 4. Perform startup zombie sweep as soon as possible
    logger.info("Performing startup zombie sweep...")
    await app.state.recovery_daemon.scan_and_reclaim()
    await app.state.recovery_daemon.scan_and_replay()

    # 5. Start Heartbeat monitor
    app.state.heartbeat_task = asyncio.create_task(_leadership_heartbeat(app))


async def _leadership_heartbeat(app: FastAPI):
    """
    Primary node heartbeat:
    - Periodically verifies connection is alive (SELECT 1)
    - Periodically verifies lock is still held (optional)
    - Terminate task if connection is lost
    """
    conn = app.state.leader_conn
    try:
        while True:
            # Confirm connection is still the same session that holds the lock
            current_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            if current_pid != app.state.db_pid:
                logger.error(f"Leadership PID mismatch: expected {app.state.db_pid}, got {current_pid} (Heartbeat Failed)")
                break
            
            logger.info(f"Leader heartbeat alive (db_pid={app.state.db_pid})")
            await asyncio.sleep(30)
    except Exception as e:
        logger.error(f"Leadership heartbeat error: {e}")
    finally:
        logger.warning("Terminating heartbeat and primary role.")


async def _leadership_watchdog(app: FastAPI):
    """
    Standby node watchdog:
    - Periodically (with jitter) attempts to acquire the lock.
    - If successful, promotes the current node to Primary.
    """
    logger.info("SECONDARY_NODE: RecoveryDaemon standby (Leader Election Watchdog Start)")
    promotion_start = time.monotonic()
    try:
        while True:
            # Advisory lock attempt
            conn = _acquire_leader_lock(engine)
            if conn:
                duration = time.monotonic() - promotion_start
                app.state.leader_conn = conn
                logger.info(f"PRIMARY_NODE: Leadership promoted (reason=lock_acquired_on_standby, latency={duration:.2f}s)")
                await _start_primary_services(app)
                return  # Exit watchdog upon promotion success

            # Staggered polling: 4-6 seconds to avoid DB burst/thundering herd
            await asyncio.sleep(random.uniform(4, 6))
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Leadership watchdog shutting down.")
    except Exception as e:
        logger.error(f"Leadership watchdog error: {e}")
 
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info("Starting LoadMatch application.")

    app.state.executor = ThreadPoolExecutor(max_workers=64)
    logger.info("ThreadPoolExecutor(max_workers=64) attached to app.state.executor")

    app.state.event_bus_task = asyncio.create_task(EventBus.flush())
    
    # Leader election: Attempt to acquire advisory lock on a persistent connection
    app.state.leader_conn = _acquire_leader_lock(engine)
    
    if app.state.leader_conn:
        await _start_primary_services(app)
    else:
        app.state.watchdog_task = asyncio.create_task(_leadership_watchdog(app))

    yield

    recovery_task = getattr(app.state, "recovery_task", None)
    if recovery_task:
        recovery_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery_task

    heartbeat_task = getattr(app.state, "heartbeat_task", None)
    if heartbeat_task:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    watchdog_task = getattr(app.state, "watchdog_task", None)
    if watchdog_task:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task

    event_bus_task = getattr(app.state, "event_bus_task", None)
    if event_bus_task:
        event_bus_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await event_bus_task

    executor = getattr(app.state, "executor", None)
    if executor:
        executor.shutdown(wait=False)

    logger.info("Application winding down.")
    
    # Explicitly release leader lock and close connection
    leader_conn = getattr(app.state, "leader_conn", None)
    if leader_conn:
        try:
            LOCK_ID = 918273645
            leader_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
            logger.info("Leader lock explicitly released.")
        except Exception as e:
            logger.warning(f"Error releasing leader lock: {e}")
        finally:
            leader_conn.close()
            logger.info("Leader connection closed.")

app = FastAPI(title="loadmatch", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers are consolidated above
# ...
app.include_router(webhook.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(debug.router)

@app.get("/health")
async def health_check():
    status = {"service": "loadmatch", "status": "ok"}

    # DB check
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        status["db"] = "ok"
    except Exception:
        status["db"] = "fail"
        status["status"] = "degraded"

    if environment.redis_enabled():
        status["redis"] = "ok" if get_redis_client() is not None else "fail"
        if status["redis"] == "fail":
            status["status"] = "degraded"
    else:
        status["redis"] = "disabled"

    status["anthropic"] = "configured" if environment.anthropic_enabled() else "disabled"
    status["whatsapp"] = "configured" if environment.whatsapp_enabled() else "disabled"

    return status
