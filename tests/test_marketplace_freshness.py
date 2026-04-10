from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.enums import Intent
from app.contracts.payloads import GenericActionPayload
from app.models.enums import ListingStatus, LoadRequestStatus, KycFlowState
from app.services.dispatcher_service import DispatcherService
from app.services.matching_service import (
    canonical_lane_key,
    find_matches_for_load,
    find_matches_for_truck_summary,
    find_recent_duplicate_listing,
)


def _query_all(rows):
    query = MagicMock()
    query.filter.return_value = query
    query.join.return_value = query
    query.all.return_value = rows
    return query


def _owner(owner_id: str):
    return SimpleNamespace(
        id=owner_id,
        name="Owner",
        phone="919999999999",
        rating=4.8,
        completed_trips=10,
        kyc_flow_state=KycFlowState.verified,
    )


def _listing(owner_id: str, created_at: datetime, *, from_city: str = "Delhi", to_city: str = "Jaipur"):
    return SimpleNamespace(
        id=f"listing-{owner_id}-{from_city}-{to_city}",
        owner_id=owner_id,
        from_city=from_city,
        to_city=to_city,
        departure_date=date.today(),
        available_capacity_kg=7000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=created_at,
        status=ListingStatus.open,
    )


def _load(shipper_id: str, created_at: datetime, *, from_city: str = "Delhi", to_city: str = "Jaipur"):
    return SimpleNamespace(
        id=f"load-{shipper_id}-{from_city}-{to_city}",
        shipper_id=shipper_id,
        from_city=from_city,
        to_city=to_city,
        pickup_date=date.today(),
        weight_kg=5000,
        category=None,
        created_at=created_at,
        status=LoadRequestStatus.open,
    )


def test_same_user_duplicate_lane_within_window_reuses_existing_listing():
    now = datetime.now(timezone.utc)
    existing_listing = _listing("user-123", now, from_city="Delhi", to_city="Jaipur")
    db = MagicMock()
    db.query.return_value = _query_all([existing_listing])
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = GenericActionPayload(
        action="POST_TRUCK",
        data={
            "current_city": "delhi",
            "to_city": "jaipur",
            "capacity_kg": 7000,
            "departure_date": "tomorrow",
        },
    )

    with patch("app.services.dispatcher_service.find_matches_for_truck_summary", return_value={"match_count": 0, "matches": []}):
        response = dispatcher._handle_confirm_truck(payload)

    assert "Truck Posted" in response.text
    assert "Delhi" in response.text
    added_records = [call.args[0] for call in db.add.call_args_list]
    for record in added_records:
        assert type(record).__name__ != "TruckSpaceListing"


def test_listing_persists_canonical_lane_key_on_create():
    db = MagicMock()
    db.flush.return_value = None
    truck_query = MagicMock()
    truck_query.filter.return_value = truck_query
    truck_query.first.return_value = None
    listing_query = MagicMock()
    listing_query.filter.return_value = listing_query
    listing_query.all.return_value = []
    db.query.side_effect = [listing_query, truck_query]
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = GenericActionPayload(
        action="POST_TRUCK",
        data={
            "current_city": "blr",
            "to_city": "delhi",
            "capacity_kg": 7000,
            "departure_date": "tomorrow",
        },
    )

    with patch("app.services.dispatcher_service.find_matches_for_truck_summary", return_value={"match_count": 0, "matches": []}):
        response = dispatcher.execute(Intent.CONFIRM, payload, current_workflow="TRUCK_FLOW")

    added_objects = [call.args[0] for call in db.add.call_args_list]
    listing = next(obj for obj in added_objects if hasattr(obj, "canonical_lane_key"))
    assert listing.canonical_lane_key == "bangalore:delhi"
    assert "Truck Posted" in response.text


def test_duplicate_lane_allowed_after_window():
    stale_listing = _listing(
        "user-123",
        datetime.now(timezone.utc) - timedelta(minutes=20),
        from_city="Delhi",
        to_city="Jaipur",
    )
    db = MagicMock()
    db.query.return_value = _query_all([stale_listing])

    duplicate = find_recent_duplicate_listing(db, "user-123", "delhi", "jaipur")

    assert duplicate is None


def test_duplicate_helper_prefers_persisted_canonical_lane_key():
    now = datetime.now(timezone.utc)
    listing = _listing("user-123", now, from_city="Delhi NCR", to_city="Jaipur")
    listing.canonical_lane_key = "delhi:jaipur"
    db = MagicMock()
    db.query.return_value = _query_all([listing])

    duplicate = find_recent_duplicate_listing(db, "user-123", "delhi", "jaipur")

    assert duplicate is listing


def test_stale_listing_not_returned_by_matcher():
    now = datetime.now(timezone.utc)
    stale_listing = _listing(
        "user-456",
        now - timedelta(hours=2),
        from_city="Delhi",
        to_city="Jaipur",
    )
    load = _load("user-123", now, from_city="Delhi", to_city="Jaipur")
    db = MagicMock()
    db.query.return_value = _query_all([(stale_listing, _owner("user-456"))])
    db.flush.return_value = None

    with patch("app.services.matching_service.suggest_nearby_matches", return_value=[]):
        matches = find_matches_for_load(db, load, commit=False)

    assert matches == []


def test_same_user_not_matched_to_own_listing():
    now = datetime.now(timezone.utc)
    listing = _listing("user-123", now, from_city="Delhi", to_city="Jaipur")
    load = _load("user-123", now, from_city="Delhi", to_city="Jaipur")
    db = MagicMock()
    db.query.return_value = _query_all([(load, _owner("user-123"))])

    matches = find_matches_for_truck_summary(db, listing)

    assert matches["match_count"] == 0
    assert matches["matches"] == []


def test_lane_key_alias_canonicalization_consistency():
    assert canonical_lane_key("blr", "delhi") == "bangalore:delhi"
    assert canonical_lane_key("delhi", "bangalore") == "bangalore:delhi"
