import os
import uuid
from datetime import date, datetime, timedelta, timezone

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

from app.database import Base
from app.models.enums import ListingStatus, LoadRequestStatus, TruckType, UserRole
from app.models.event import EventLog
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.processed_message import ProcessedMessage
from app.models.truck import Truck
from app.models.user import User
from app.services.internal_monitoring_service import InternalMonitoringService


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _create_user(db, suffix: str, role: UserRole) -> User:
    user = User(
        id=uuid.uuid4(),
        phone=f"91900000{suffix}",
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def _create_truck(db, owner_id, suffix: str, truck_type: TruckType) -> Truck:
    truck = Truck(
        id=uuid.uuid4(),
        owner_id=owner_id,
        registration_number=f"MH01{suffix}",
        truck_type=truck_type,
        total_capacity_kg=9000,
    )
    db.add(truck)
    db.flush()
    return truck


def test_constraint_drift_metrics_detect_authority_violations(session):
    now = datetime.now(timezone.utc)
    transporter = _create_user(session, "1001", UserRole.transporter)
    shipper = _create_user(session, "1002", UserRole.shipper)
    truck = _create_truck(session, transporter.id, "A1001", TruckType.medium)

    # canonical_lane_key NULL + vehicle_type NULL on listing
    session.add(
        TruckSpaceListing(
            id=uuid.uuid4(),
            truck_id=truck.id,
            owner_id=transporter.id,
            from_city="Delhi",
            to_city="Jaipur",
            canonical_lane_key=None,
            vehicle_type=None,
            departure_date=date.today(),
            total_capacity_kg=9000,
            available_capacity_kg=9000,
            price_per_kg=10,
            status=ListingStatus.open,
        )
    )

    # canonical_lane_key NULL + vehicle_type NULL on load
    session.add(
        LoadRequest(
            id=uuid.uuid4(),
            shipper_id=shipper.id,
            from_city="Delhi",
            to_city="Jaipur",
            canonical_lane_key=None,
            vehicle_type=None,
            pickup_date=date.today(),
            weight_kg=5000,
            status=LoadRequestStatus.open,
        )
    )

    # duplicate active load group on shipper+lane+vehicle
    for load_id in ["dup-r1", "dup-r2"]:
        session.add(
            LoadRequest(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, load_id),
                shipper_id=shipper.id,
                from_city="Delhi",
                to_city="Mumbai",
                canonical_lane_key="delhi:mumbai",
                vehicle_type=TruckType.medium.value,
                pickup_date=date.today(),
                weight_kg=5000,
                status=LoadRequestStatus.open,
            )
        )

    # stale EXECUTING row + request_payload NULL
    session.add(
        ProcessedMessage(
            id=uuid.uuid4(),
            wamid="wamid.stale",
            idempotency_key="shipper:confirm:wamid.stale",
            status="SUCCESS",
            delivery_state="EXECUTING",
            updated_at=now - timedelta(minutes=20),
            replay_execution_hash="hash-1",
            request_payload=None,
            expires_at=now + timedelta(days=1),
        )
    )
    session.commit()

    metrics = InternalMonitoringService.get_constraint_drift_metrics(session, now=now)

    assert metrics["listing_canonical_lane_key_null_count"] >= 1
    assert metrics["listing_vehicle_type_null_count"] >= 1
    assert metrics["load_canonical_lane_key_null_count"] >= 1
    assert metrics["load_vehicle_type_null_count"] >= 1
    # unique constraint should enforce zero active listing duplicate groups
    assert metrics["duplicate_active_listing_groups"] == 0
    assert metrics["duplicate_active_load_groups"] >= 1
    assert metrics["stale_executing_count"] >= 1
    assert metrics["request_payload_null_count"] >= 1
    assert metrics["total_violations"] >= 7


def test_liquidity_health_snapshot_reports_supply_demand_and_suppression_rates(session):
    now = datetime.now(timezone.utc)
    transporter = _create_user(session, "2001", UserRole.transporter)
    shipper = _create_user(session, "2002", UserRole.shipper)
    truck = _create_truck(session, transporter.id, "A2001", TruckType.medium)

    listings = [
        ("delhi:jaipur", TruckType.medium.value),
        ("delhi:mumbai", TruckType.medium.value),
        ("delhi:mumbai", TruckType.trailer.value),
    ]
    for idx, (lane, vehicle_type) in enumerate(listings, start=1):
        session.add(
            TruckSpaceListing(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, f"listing-{idx}"),
                truck_id=truck.id,
                owner_id=transporter.id,
                from_city="Delhi",
                to_city="Jaipur" if lane.endswith("jaipur") else "Mumbai",
                canonical_lane_key=lane,
                vehicle_type=vehicle_type,
                departure_date=date.today(),
                total_capacity_kg=9000,
                available_capacity_kg=9000,
                price_per_kg=10,
                status=ListingStatus.open,
            )
        )

    loads = [
        ("delhi:jaipur", TruckType.medium.value),
        ("delhi:mumbai", TruckType.trailer.value),
    ]
    for idx, (lane, vehicle_type) in enumerate(loads, start=1):
        session.add(
            LoadRequest(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, f"load-{idx}"),
                shipper_id=shipper.id,
                from_city="Delhi",
                to_city="Jaipur" if lane.endswith("jaipur") else "Mumbai",
                canonical_lane_key=lane,
                vehicle_type=vehicle_type,
                pickup_date=date.today(),
                weight_kg=5000,
                status=LoadRequestStatus.open,
            )
        )

    event_rows = [
        ("LOAD_CONFIRM_ATTEMPTED", 4),
        ("LOAD_FRESHNESS_WINDOW_BLOCKED", 1),
        ("LISTING_CONFIRM_ATTEMPTED", 2),
        ("LISTING_FRESHNESS_WINDOW_BLOCKED", 1),
    ]
    for event_type, count in event_rows:
        for idx in range(count):
            session.add(
                EventLog(
                    id=uuid.uuid5(uuid.NAMESPACE_DNS, f"{event_type}-{idx}"),
                    user_id=transporter.id,
                    event_type=event_type,
                    data={},
                    created_at=now - timedelta(minutes=30),
                )
            )

    session.commit()

    snapshot = InternalMonitoringService.get_liquidity_health_snapshot(session, now=now)

    assert snapshot["active_lanes"] == 2
    assert snapshot["vehicle_segments"] == 2
    assert snapshot["avg_lane_supply"] == 1.5
    assert snapshot["avg_lane_demand"] == 1.0
    assert snapshot["imbalance_ratio"] == pytest.approx(0.6667, abs=1e-4)
    assert snapshot["freshness_suppression_rate"] == pytest.approx(0.3333, abs=1e-4)
    assert snapshot["duplicate_load_reuse_rate"] == pytest.approx(0.25, abs=1e-4)
    assert snapshot["duplicate_listing_reuse_rate"] == pytest.approx(0.5, abs=1e-4)
