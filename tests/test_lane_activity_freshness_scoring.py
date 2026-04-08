from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.enums import KycFlowState, ListingStatus, LoadRequestStatus
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.match import Match
from app.services.matching_service import (
    compute_lane_activity_freshness_bonus,
    rank_matches,
)


def _owner(owner_id: str = "owner-1", rating: float = 3.6):
    return SimpleNamespace(
        id=owner_id,
        name="Owner",
        phone="919999999999",
        rating=rating,
        completed_trips=10,
        kyc_flow_state=KycFlowState.verified,
    )


def _load():
    return SimpleNamespace(
        id="load-1",
        shipper_id="shipper-1",
        from_city="Delhi",
        to_city="Jaipur",
        pickup_date=date.today(),
        weight_kg=5000,
        category=None,
        corridor_source=None,
    )


def _listing_row(
    listing_id: str,
    *,
    canonical_lane_key: str = "delhi:jaipur",
    created_at: datetime | None = None,
    status=ListingStatus.open,
):
    return SimpleNamespace(
        id=listing_id,
        owner_id="owner-1",
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key=canonical_lane_key,
        departure_date=date.today() + timedelta(days=5),
        available_capacity_kg=5000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=created_at or datetime.now(timezone.utc),
        status=status,
    )


def _load_row(
    load_id: str,
    *,
    from_city: str = "Delhi",
    to_city: str = "Jaipur",
    created_at: datetime | None = None,
    status=LoadRequestStatus.open,
):
    return SimpleNamespace(
        id=load_id,
        shipper_id="shipper-1",
        from_city=from_city,
        to_city=to_city,
        pickup_date=date.today(),
        weight_kg=5000,
        category=None,
        created_at=created_at or datetime.now(timezone.utc),
        status=status,
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


def _ranking_session(match_ids: list[str]):
    match_queries = []
    for match_id in match_ids:
        match_query = MagicMock()
        match_query.filter.return_value = match_query
        match_query.first.return_value = SimpleNamespace(
            id=match_id,
            listing_id=f"listing-{match_id}",
            load_request_id="load-1",
            match_score=0,
        )
        match_queries.append(match_query)

    session = MagicMock()
    session.flush.return_value = None

    def query_side_effect(model):
        if model is Match:
            return match_queries.pop(0)
        raise AssertionError(f"Unexpected query model: {model}")

    session.query.side_effect = query_side_effect
    return session


def test_recent_lane_activity_gets_high_bonus():
    session = _activity_session(
        [_listing_row("listing-1", created_at=datetime.now(timezone.utc) - timedelta(minutes=10))],
        [],
    )

    assert compute_lane_activity_freshness_bonus(session, "delhi:jaipur") == 6


def test_medium_recent_activity_gets_medium_bonus():
    session = _activity_session(
        [],
        [_load_row("load-1", created_at=datetime.now(timezone.utc) - timedelta(minutes=90))],
    )

    assert compute_lane_activity_freshness_bonus(session, "delhi:jaipur") == 3


def test_old_activity_gets_small_bonus():
    session = _activity_session(
        [_listing_row("listing-1", created_at=datetime.now(timezone.utc) - timedelta(hours=4))],
        [],
    )

    assert compute_lane_activity_freshness_bonus(session, "delhi:jaipur") == 1


def test_no_activity_returns_zero_bonus():
    session = _activity_session([], [])

    assert compute_lane_activity_freshness_bonus(session, "delhi:jaipur") == 0


def test_bonus_affects_ranking_order():
    owner = _owner()
    load = _load()
    low_activity_listing = _listing_row("listing-a", canonical_lane_key="delhi:mumbai")
    high_activity_listing = _listing_row("listing-b", canonical_lane_key="delhi:jaipur")
    session = _ranking_session(["match-a", "match-b"])

    with patch(
        "app.services.matching_service.compute_lane_activity_freshness_bonus",
        side_effect=lambda _session, lane_key, **_kwargs: 6 if lane_key == "delhi:jaipur" else 0,
    ), patch(
        "app.services.matching_service.compute_lane_liquidity_score",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_lane_demand_score",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_supply_demand_ratio_score",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_operator_reliability_bonus",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_lane_specific_reliability_bonus",
        return_value=0,
    ):
        ranked = rank_matches(
            load,
            [(low_activity_listing, owner), (high_activity_listing, owner)],
            session,
        )

    assert [result["match_id"] for result in ranked] == ["match-b", "match-a"]
