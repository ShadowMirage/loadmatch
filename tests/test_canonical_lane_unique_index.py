import os
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, inspect
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
from app.models.enums import ListingStatus, TruckType, UserRole
from app.models.listing import TruckSpaceListing
from app.models.truck import Truck
from app.models.user import User


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


def _create_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        phone="917777770001",
        role=UserRole.transporter,
    )
    db.add(user)
    db.flush()
    return user


def _create_truck(db, owner_id, registration_number: str, truck_type: TruckType = TruckType.medium) -> Truck:
    truck = Truck(
        id=uuid.uuid4(),
        owner_id=owner_id,
        truck_type=truck_type,
        total_capacity_kg=9000,
        registration_number=registration_number,
    )
    db.add(truck)
    db.flush()
    return truck


def _create_listing(db, owner_id, truck_id, lane_key: str, vehicle_type: str = TruckType.medium.value) -> TruckSpaceListing:
    listing = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=owner_id,
        truck_id=truck_id,
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key=lane_key,
        vehicle_type=vehicle_type,
        departure_date=date.today(),
        total_capacity_kg=9000,
        available_capacity_kg=9000,
        price_per_kg=10,
        status=ListingStatus.open,
    )
    db.add(listing)
    db.flush()
    return listing


def test_unique_user_lane_vehicle_open_index_present(session):
    indexes = inspect(session.bind).get_indexes("truck_space_listings")
    assert any(index["name"] == "unique_user_lane_vehicle_open" for index in indexes)


def test_unique_user_lane_vehicle_open_index_blocks_duplicate_insert(session):
    user = _create_user(session)
    truck_a = _create_truck(session, user.id, "MH14AA1111", truck_type=TruckType.medium)
    truck_b = _create_truck(session, user.id, "MH14AA2222", truck_type=TruckType.medium)
    _create_listing(session, user.id, truck_a.id, "delhi:jaipur", vehicle_type=TruckType.medium.value)
    session.commit()

    duplicate = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=user.id,
        truck_id=truck_b.id,
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


def test_unique_user_lane_vehicle_open_index_allows_parallel_vehicle_types(session):
    user = _create_user(session)
    truck_a = _create_truck(session, user.id, "MH14AA3333", truck_type=TruckType.medium)
    truck_b = _create_truck(session, user.id, "MH14AA4444", truck_type=TruckType.trailer)
    _create_listing(session, user.id, truck_a.id, "delhi:jaipur", vehicle_type=TruckType.medium.value)
    session.commit()

    parallel = TruckSpaceListing(
        id=uuid.uuid4(),
        owner_id=user.id,
        truck_id=truck_b.id,
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key="delhi:jaipur",
        vehicle_type=TruckType.trailer.value,
        departure_date=date.today(),
        total_capacity_kg=9000,
        available_capacity_kg=9000,
        price_per_kg=10,
        status=ListingStatus.open,
    )
    session.add(parallel)
    session.flush()
