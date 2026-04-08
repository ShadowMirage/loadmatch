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
from app.database import Base
from app.contracts.enums import Intent
from app.contracts.payloads import GenericActionPayload
from app.models.enums import ListingStatus, TruckType, UserRole
from app.models.listing import TruckSpaceListing
from app.models.truck import Truck
from app.models.user import User
from app.services.dispatcher_service import DispatcherService
from app.services.marketplace_freshness_service import is_recent_duplicate_lane


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
        phone=f"91888888{suffix}",
        role=UserRole.transporter,
    )
    db.add(user)
    db.flush()
    return user


def _create_truck(db, owner_id, suffix: str, truck_type: TruckType = TruckType.medium) -> Truck:
    truck = Truck(
        id=uuid.uuid4(),
        owner_id=owner_id,
        truck_type=truck_type,
        total_capacity_kg=8000,
        registration_number=f"DL01{suffix}",
    )
    db.add(truck)
    db.flush()
    return truck


def _create_listing(
    db,
    *,
    owner_id,
    truck_id,
    canonical_lane_key,
    vehicle_type=TruckType.medium.value,
    from_city="Delhi",
    to_city="Jaipur",
    status=ListingStatus.open,
    created_at=None,
):
    listing = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=owner_id,
        truck_id=truck_id,
        from_city=from_city,
        to_city=to_city,
        canonical_lane_key=canonical_lane_key,
        vehicle_type=vehicle_type,
        departure_date=date.today(),
        total_capacity_kg=8000,
        available_capacity_kg=8000,
        price_per_kg=10,
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(listing)
    db.flush()
    return listing


def test_duplicate_lane_blocked_within_window(session):
    user = _create_user(session, "1001")
    truck = _create_truck(session, user.id, "A101")
    listing = _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.commit()

    duplicate = is_recent_duplicate_lane(
        session,
        user.id,
        "delhi:jaipur",
        TruckType.medium.value,
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate.id == listing.id


def test_duplicate_lane_allowed_after_window_expires(session):
    user = _create_user(session, "1002")
    truck = _create_truck(session, user.id, "A102")
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES + 5),
    )
    session.commit()

    duplicate = is_recent_duplicate_lane(
        session,
        user.id,
        "delhi:jaipur",
        TruckType.medium.value,
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate is None


def test_different_lane_not_blocked(session):
    user = _create_user(session, "1003")
    truck = _create_truck(session, user.id, "A103")
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
    )
    session.commit()

    duplicate = is_recent_duplicate_lane(
        session,
        user.id,
        "delhi:mumbai",
        TruckType.medium.value,
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate is None


def test_partial_backfill_rows_use_runtime_lane_identity(session):
    user = _create_user(session, "1004")
    truck = _create_truck(session, user.id, "A104")
    listing = _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key=None,
        vehicle_type=TruckType.medium.value,
        from_city="Delhi NCR",
        to_city="Jaipur",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.commit()

    duplicate = is_recent_duplicate_lane(
        session,
        user.id,
        "delhi:jaipur",
        TruckType.medium.value,
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert duplicate.id == listing.id


def test_vehicle_type_parallel_lane_allowed(session):
    user = _create_user(session, "1005")
    medium_truck = _create_truck(session, user.id, "A105", truck_type=TruckType.medium)
    trailer_truck = _create_truck(session, user.id, "A106", truck_type=TruckType.trailer)
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=medium_truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.commit()

    dispatcher = DispatcherService(session, user_id=user.id)
    payload = GenericActionPayload(
        action="POST_TRUCK",
        data={
            "current_city": "delhi",
            "to_city": "jaipur",
            "capacity_kg": 8000,
            "departure_date": "tomorrow",
            "truck_type": TruckType.trailer.value,
        },
    )

    with patch("app.services.dispatcher_service.logger.info"), \
         patch("app.services.dispatcher_service.find_matches_for_truck_summary", return_value={"match_count": 0, "matches": []}):
        response = dispatcher.execute(Intent.CONFIRM, payload, current_workflow="TRUCK_FLOW")

    assert "Truck Posted" in response.text
    assert session.query(TruckSpaceListing).count() == 2


def test_freshness_window_vehicle_specific_scope(session):
    user = _create_user(session, "1006")
    truck = _create_truck(session, user.id, "A107", truck_type=TruckType.medium)
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.commit()

    trailer_duplicate = is_recent_duplicate_lane(
        session,
        user.id,
        "delhi:jaipur",
        TruckType.trailer.value,
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )
    medium_duplicate = is_recent_duplicate_lane(
        session,
        user.id,
        "delhi:jaipur",
        TruckType.medium.value,
        settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
    )

    assert trailer_duplicate is None
    assert medium_duplicate is not None
