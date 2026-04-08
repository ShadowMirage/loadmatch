from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.enums import KycFlowState
from app.models.match import Match
from app.services.matching_service import (
    compute_lane_specific_reliability_bonus,
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


def _listing(
    listing_id: str,
    *,
    owner_id: str = "owner-1",
    canonical_lane_key: str = "delhi:jaipur",
):
    return SimpleNamespace(
        id=listing_id,
        owner_id=owner_id,
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key=canonical_lane_key,
        departure_date=date.today() + timedelta(days=5),
        available_capacity_kg=5000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )


def _count_session(match_count: int):
    match_query = MagicMock()
    match_query.join.return_value = match_query
    match_query.filter.return_value = match_query
    match_query.count.return_value = match_count

    session = MagicMock()
    session.query.return_value = match_query
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


def test_no_lane_history_returns_zero_bonus():
    assert compute_lane_specific_reliability_bonus(_count_session(0), "owner-1", "delhi:jaipur") == 0


def test_small_lane_history_returns_small_bonus():
    assert compute_lane_specific_reliability_bonus(_count_session(2), "owner-1", "delhi:jaipur") == 2


def test_large_lane_history_returns_medium_bonus():
    assert compute_lane_specific_reliability_bonus(_count_session(4), "owner-1", "delhi:jaipur") == 4


def test_bonus_affects_ranking_order():
    load = _load()
    owner = _owner()
    low_lane_history_listing = _listing("listing-a", owner_id="owner-1", canonical_lane_key="delhi:mumbai")
    high_lane_history_listing = _listing("listing-b", owner_id="owner-1", canonical_lane_key="delhi:jaipur")
    session = _ranking_session(["match-a", "match-b"])

    with patch(
        "app.services.matching_service.compute_lane_specific_reliability_bonus",
        side_effect=lambda _session, _owner_id, lane_key, **_kwargs: 6 if lane_key == "delhi:jaipur" else 0,
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
        "app.services.matching_service.compute_lane_activity_freshness_bonus",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_operator_reliability_bonus",
        return_value=0,
    ):
        ranked = rank_matches(
            load,
            [(low_lane_history_listing, owner), (high_lane_history_listing, owner)],
            session,
        )

    assert [result["match_id"] for result in ranked] == ["match-b", "match-a"]
