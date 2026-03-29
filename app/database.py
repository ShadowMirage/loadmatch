import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.runtime import environment

logger = logging.getLogger(__name__)


def _prepare_sqlite_filesystem(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return

    database = url.database
    if not database or database == ":memory:":
        return

    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def build_engine_kwargs(database_url: str) -> dict:
    url = make_url(database_url)
    engine_kwargs = {"pool_pre_ping": True}

    if url.get_backend_name() == "sqlite":
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        if url.database in (None, "", ":memory:"):
            engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs.update(pool_size=10, max_overflow=20)

    return engine_kwargs


def build_engine(database_url: str) -> Engine:
    _prepare_sqlite_filesystem(database_url)
    return create_engine(database_url, **build_engine_kwargs(database_url))


SessionLocal = sessionmaker(autocommit=False, autoflush=False)


def configure_engine(database_url: str) -> Engine:
    global engine
    engine = build_engine(database_url)
    SessionLocal.configure(bind=engine)
    return engine


def resolve_database_url() -> str:
    configured_url = os.getenv("DATABASE_URL") or settings.DATABASE_URL
    if environment.postgres_enabled():
        return configured_url
    return configured_url if configured_url.startswith("sqlite") else settings.LOCAL_DATABASE_URL


engine = configure_engine(resolve_database_url())

Base = declarative_base()


def load_model_registry() -> None:
    """
    Import the model package so all declarative mappings are registered
    before metadata operations run.
    """
    import app.models  # noqa: F401


def create_schema(bind: Engine | None = None) -> None:
    """
    Create the full schema for the currently configured engine or an override.
    """
    target_engine = bind or engine
    if target_engine.dialect.name == "sqlite":
        import sqlalchemy.dialects.sqlite.base as sqlite_base

        if not hasattr(sqlite_base.SQLiteTypeCompiler, "visit_JSONB"):
            sqlite_base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

    load_model_registry()
    try:
        Base.metadata.create_all(bind=target_engine)
    except OperationalError as exc:
        if bind is not None or not settings.AUTO_FALLBACK_TO_SQLITE:
            raise

        target_url = target_engine.url
        fallback_host = getattr(target_url, "host", None)
        if fallback_host not in {"db", "postgres", "postgresql"}:
            raise

        logger.warning(
            "Primary database bootstrap failed for %s. Falling back to %s. Error=%s",
            target_url.render_as_string(hide_password=True),
            settings.LOCAL_DATABASE_URL,
            exc,
        )
        target_engine = configure_engine(settings.LOCAL_DATABASE_URL)
        if target_engine.dialect.name == "sqlite":
            import sqlalchemy.dialects.sqlite.base as sqlite_base

            if not hasattr(sqlite_base.SQLiteTypeCompiler, "visit_JSONB"):
                sqlite_base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

        load_model_registry()
        Base.metadata.create_all(bind=target_engine)


def drop_schema(bind: Engine | None = None) -> None:
    """
    Drop the full schema for the currently configured engine or an override.
    """
    load_model_registry()
    Base.metadata.drop_all(bind=bind or engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
