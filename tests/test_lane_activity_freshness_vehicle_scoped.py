from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.services.matching_service import compute_lane_activity_freshness_bonus


def _listing_row(
    *,
    canonical_lane_key: str = "delhi:jaipur",
    vehicle_type: str | None = None,
    created_at: datetime | None = None,
):
    return SimpleNamespace(
        id="listing-1",
        canonical_lane_key=canonical_lane_key,
        vehicle_type=vehicle_type,
        from_city="Delhi",
        to_city="Jaipur",
        created_at=created_at or datetime.now(timezone.utc),
    )


def _load_row(
    *,
    from_city: str = "Delhi",
    to_city: str = "Jaipur",
    vehicle_type: str | None = None,
    created_at: datetime | None = None,
):
    return SimpleNamespace(
        id="load-1",
        from_city=from_city,
        to_city=to_city,
        vehicle_type=vehicle_type,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _activity_session(listings, loads):
    listing_query = MagicMock()
    listing_query.filter.return_value = listing_query
    listing_query.all.return_value = listings

    load_query = MagicMock()
    load_query.filter.return_value = load_query
    load_query.all.return_value = loads

    session = MagicMock()

    def query_side_effect(model):
        if model is TruckSpaceListing:
            return listing_query
        if model is LoadRequest:
            return load_query
        raise AssertionError(f"Unexpected query model: {model}")

    session.query.side_effect = query_side_effect
    return session


def test_vehicle_specific_lane_activity_bonus():
    now = datetime.now(timezone.utc)
    session = _activity_session(
        [
            _listing_row(vehicle_type="medium", created_at=now - timedelta(minutes=10)),
            _listing_row(vehicle_type="trailer", created_at=now - timedelta(hours=4)),
        ],
        [],
    )

    assert (
        compute_lane_activity_freshness_bonus(session, "delhi:jaipur", vehicle_type="medium", now=now)
        == 6
    )
    assert (
        compute_lane_activity_freshness_bonus(session, "delhi:jaipur", vehicle_type="trailer", now=now)
        == 1
    )


def test_lane_activity_vehicle_fallback_when_missing():
    now = datetime.now(timezone.utc)
    session = _activity_session(
        [_listing_row(vehicle_type=None, created_at=now - timedelta(minutes=90))],
        [_load_row(vehicle_type=None, created_at=now - timedelta(minutes=100))],
    )

    # No typed lane activity exists yet, so vehicle-scoped query falls back to lane-level freshness.
    assert (
        compute_lane_activity_freshness_bonus(session, "delhi:jaipur", vehicle_type="medium", now=now)
        == 3
    )


def test_activity_bonus_deterministic_per_pass():
    session = _activity_session([_listing_row(vehicle_type="medium")], [])
    score_context = {}

    score_a = compute_lane_activity_freshness_bonus(
        session,
        "delhi:jaipur",
        vehicle_type="medium",
        score_context=score_context,
    )
    score_b = compute_lane_activity_freshness_bonus(
        session,
        "delhi:jaipur",
        vehicle_type="medium",
        score_context=score_context,
    )

    assert score_a == 6
    assert score_b == 6
    assert session.query.call_count == 2
