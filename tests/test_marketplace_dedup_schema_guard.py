import os
import uuid
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
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

from app.database import Base
from app.contracts.enums import Intent
from app.contracts.payloads import GenericActionPayload
from app.models.enums import ListingStatus, TruckType, UserRole
from app.models.event import EventLog
from app.models.listing import TruckSpaceListing
from app.models.truck import Truck
from app.models.user import User
from app.services.dispatcher_service import DispatcherService
from app.services.matching_service import find_recent_duplicate_listing


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
        phone=f"91999999{suffix}",
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
        total_capacity_kg=9000,
        registration_number=f"MH12{suffix}",
    )
    db.add(truck)
    db.flush()
    return truck


def _create_listing(
    db,
    *,
    owner_id,
    truck_id,
    canonical_lane_key: str | None,
    vehicle_type: str = TruckType.medium.value,
    from_city: str = "Delhi",
    to_city: str = "Jaipur",
    status: ListingStatus = ListingStatus.open,
) -> TruckSpaceListing:
    listing = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=owner_id,
        truck_id=truck_id,
        from_city=from_city,
        to_city=to_city,
        canonical_lane_key=canonical_lane_key,
        vehicle_type=vehicle_type,
        departure_date=date.today(),
        total_capacity_kg=9000,
        available_capacity_kg=9000,
        price_per_kg=10,
        status=status,
    )
    db.add(listing)
    db.flush()
    return listing


def test_duplicate_lane_insert_blocked_by_index(session):
    user = _create_user(session, "0001")
    truck = _create_truck(session, user.id, "A111")
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        status=ListingStatus.open,
    )
    session.commit()

    duplicate = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=user.id,
        truck_id=truck.id,
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        departure_date=date.today(),
        total_capacity_kg=9000,
        available_capacity_kg=9000,
        price_per_kg=10,
        status=ListingStatus.partial,
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.flush()


def test_same_lane_same_vehicle_duplicate_blocked(session):
    user = _create_user(session, "0009")
    truck = _create_truck(session, user.id, "A999", truck_type=TruckType.medium)
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        status=ListingStatus.open,
    )
    session.commit()

    duplicate = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=user.id,
        truck_id=truck.id,
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        departure_date=date.today(),
        total_capacity_kg=9000,
        available_capacity_kg=9000,
        price_per_kg=10,
        status=ListingStatus.partial,
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_lane_insert_returns_existing_listing(session):
    user = _create_user(session, "0002")
    truck = _create_truck(session, user.id, "A222")
    existing = _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        status=ListingStatus.open,
    )
    session.commit()

    dispatcher = DispatcherService(session, user_id=user.id)
    payload = GenericActionPayload(
        action="POST_TRUCK",
        data={
            "current_city": "delhi",
            "to_city": "jaipur",
            "capacity_kg": 9000,
            "departure_date": "tomorrow",
        },
    )

    with patch("app.services.dispatcher_service.is_recent_duplicate_lane", return_value=None), \
         patch(
             "app.services.dispatcher_service.find_matches_for_truck_summary",
             return_value={"match_count": 0, "matches": []},
         ), \
         patch("app.services.dispatcher_service.logger.info") as mock_info:
        response = dispatcher.execute(Intent.CONFIRM, payload, current_workflow="TRUCK_FLOW")

    assert "Truck Posted" in response.text
    assert str(existing.available_capacity_kg) in response.text
    assert session.query(TruckSpaceListing).count() == 1
    schema_calls = [call for call in mock_info.call_args_list if call.args and call.args[0] == "SCHEMA_DUPLICATE_LANE_SUPPRESSED"]
    assert schema_calls
    assert schema_calls[0].kwargs["extra"]["vehicle_type"] == TruckType.medium.value


def test_dispatcher_duplicate_vehicle_specific_reuse(session):
    user = _create_user(session, "0010")
    truck = _create_truck(session, user.id, "A1010", truck_type=TruckType.medium)
    existing = _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        status=ListingStatus.open,
    )
    session.commit()

    dispatcher = DispatcherService(session, user_id=user.id)
    payload = GenericActionPayload(
        action="POST_TRUCK",
        data={
            "current_city": "delhi",
            "to_city": "jaipur",
            "capacity_kg": 9000,
            "departure_date": "tomorrow",
            "truck_type": TruckType.medium.value,
        },
    )

    with patch("app.services.dispatcher_service.is_recent_duplicate_lane", return_value=None), \
         patch(
             "app.services.dispatcher_service.find_matches_for_truck_summary",
             return_value={"match_count": 0, "matches": []},
         ), \
         patch("app.services.dispatcher_service.logger.info") as mock_info:
        response = dispatcher.execute(Intent.CONFIRM, payload, current_workflow="TRUCK_FLOW")

    assert "Truck Posted" in response.text
    assert str(existing.available_capacity_kg) in response.text
    assert session.query(TruckSpaceListing).count() == 1
    event_types = {event.event_type for event in session.query(EventLog).all()}
    assert "LISTING_CONFIRM_ATTEMPTED" in event_types
    assert "SCHEMA_DUPLICATE_LANE_SUPPRESSED" in event_types
    schema_calls = [call for call in mock_info.call_args_list if call.args and call.args[0] == "SCHEMA_DUPLICATE_LANE_SUPPRESSED"]
    assert schema_calls
    assert schema_calls[0].kwargs["extra"]["vehicle_type"] == TruckType.medium.value


def test_same_lane_different_vehicle_parallel_allowed(session):
    user = _create_user(session, "0003")
    medium_truck = _create_truck(session, user.id, "A333", truck_type=TruckType.medium)
    trailer_truck = _create_truck(session, user.id, "A334", truck_type=TruckType.trailer)
    _create_listing(
        session,
        owner_id=user.id,
        truck_id=medium_truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.medium.value,
        status=ListingStatus.open,
    )
    session.commit()

    _create_listing(
        session,
        owner_id=user.id,
        truck_id=trailer_truck.id,
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.trailer.value,
        status=ListingStatus.open,
    )
    session.commit()

    assert session.query(TruckSpaceListing).count() == 2


def test_partial_backfill_rows_still_use_runtime_fallback(session):
    user = _create_user(session, "0004")
    truck = _create_truck(session, user.id, "A444")
    listing = _create_listing(
        session,
        owner_id=user.id,
        truck_id=truck.id,
        canonical_lane_key=None,
        vehicle_type=TruckType.medium.value,
        from_city="Delhi NCR",
        to_city="Jaipur",
        status=ListingStatus.open,
    )
    session.commit()

    duplicate = find_recent_duplicate_listing(session, user.id, "delhi", "jaipur")

    assert duplicate.id == listing.id
