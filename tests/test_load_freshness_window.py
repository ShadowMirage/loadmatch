import os
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sqlalchemy.dialects.sqlite.base as sqlite_base

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("AWS_ACCESS_KEY", "test-key")
os.environ.setdefault("AWS_SECRET_KEY", "test-key")
os.environ.setdefault("AWS_REGION", "ap-south-1")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ADMIN_API_KEY", "test-key")

sqlite_base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.config import settings
from app.contracts.payloads import GenericActionPayload
from app.database import Base
from app.models.enums import LoadRequestStatus, UserRole
from app.models.event import EventLog
from app.models.load_request import LoadRequest
from app.models.user import User
from app.services.dispatcher_service import DispatcherService
from app.services.load_freshness_service import is_recent_duplicate_load


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _create_user(db, suffix: str) -> User:
    user = User(
        id=uuid.uuid4(),
        phone=f"91777777{suffix}",
        role=UserRole.shipper,
    )
    db.add(user)
    db.flush()
    return user


def _create_load(
    db,
    *,
    shipper_id,
    canonical_lane_key,
    vehicle_type,
    status=LoadRequestStatus.open,
    from_city="Delhi",
    to_city="Jaipur",
    created_at=None,
):
    load = LoadRequest(
        id=uuid.uuid4(),
        shipper_id=shipper_id,
        from_city=from_city,
        to_city=to_city,
        canonical_lane_key=canonical_lane_key,
        vehicle_type=vehicle_type,
        pickup_date=date.today(),
        weight_kg=5000,
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(load)
    db.flush()
    return load


def test_same_lane_same_vehicle_recent_load_reused(session):
    user = _create_user(session, "1001")
    existing = _create_load(
        session,
        shipper_id=user.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type="medium",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.commit()

    duplicate = is_recent_duplicate_load(
        session,
        user.id,
        "delhi:jaipur",
        "medium",
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate is not None
    assert duplicate.id == existing.id


def test_different_vehicle_same_lane_allowed(session):
    user = _create_user(session, "1002")
    _create_load(
        session,
        shipper_id=user.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type="medium",
    )
    session.commit()

    duplicate = is_recent_duplicate_load(
        session,
        user.id,
        "delhi:jaipur",
        "trailer",
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate is None


def test_different_lane_same_vehicle_allowed(session):
    user = _create_user(session, "1003")
    _create_load(
        session,
        shipper_id=user.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type="medium",
    )
    session.commit()

    duplicate = is_recent_duplicate_load(
        session,
        user.id,
        "delhi:mumbai",
        "medium",
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate is None


def test_old_load_allows_new_insert(session):
    user = _create_user(session, "1004")
    _create_load(
        session,
        shipper_id=user.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type="medium",
        created_at=datetime.now(timezone.utc)
        - timedelta(minutes=settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES + 5),
    )
    session.commit()

    duplicate = is_recent_duplicate_load(
        session,
        user.id,
        "delhi:jaipur",
        "medium",
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate is None


def test_dispatcher_reuses_recent_duplicate_load(session):
    user = _create_user(session, "1005")
    existing = _create_load(
        session,
        shipper_id=user.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type="medium",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    session.commit()

    dispatcher = DispatcherService(session, user_id=user.id)
    payload = GenericActionPayload(
        action="CONFIRM",
        data={
            "from_city": "delhi",
            "to_city": "jaipur",
            "weight_kg": 5000,
            "date": "tomorrow",
            "vehicle_type": "medium",
        },
    )

    with patch(
        "app.services.dispatcher_service.find_matches_for_load_summary",
        return_value={"match_count": 0, "matches": []},
    ):
        response = dispatcher._handle_confirm_load(payload)

    loads = session.query(LoadRequest).all()
    event_types = {event.event_type for event in session.query(EventLog).all()}
    assert "Load Created" in response.text
    assert len(loads) == 1
    assert loads[0].id == existing.id
    assert "LOAD_CONFIRM_ATTEMPTED" in event_types
    assert "LOAD_FRESHNESS_WINDOW_BLOCKED" in event_types
