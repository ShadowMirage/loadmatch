from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.enums import KycFlowState, LoadRequestStatus
from app.models.match import Match
from app.models.load_request import LoadRequest
from app.services.matching_service import compute_lane_demand_score, rank_matches


def _owner(owner_id: str = "owner-1", rating: float = 3.6):
    return SimpleNamespace(
        id=owner_id,
        name="Owner",
        phone="919999999999",
        rating=rating,
        completed_trips=10,
        kyc_flow_state=KycFlowState.verified,
    )


def _load_request(
    load_id: str,
    *,
    from_city: str = "Delhi",
    to_city: str = "Jaipur",
    vehicle_type: str | None = None,
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
        vehicle_type=vehicle_type,
        created_at=created_at or datetime.now(timezone.utc),
        status=status,
    )


def _listing(
    listing_id: str,
    *,
    owner_id: str = "owner-1",
    canonical_lane_key: str = "delhi:jaipur",
    vehicle_type: str | None = None,
):
    return SimpleNamespace(
        id=listing_id,
        owner_id=owner_id,
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key=canonical_lane_key,
        vehicle_type=vehicle_type,
        departure_date=date.today() + timedelta(days=5),
        available_capacity_kg=5000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )


def _count_session(loads):
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = loads
    session = MagicMock()
    session.query.return_value = query
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

    load_query = MagicMock()
    load_query.filter.return_value = load_query
    load_query.all.return_value = []

    session = MagicMock()
    session.flush.return_value = None

    def query_side_effect(model):
        if model is Match:
            return match_queries.pop(0)
        if model is LoadRequest:
            return load_query
        raise AssertionError(f"Unexpected query model: {model}")

    session.query.side_effect = query_side_effect
    return session


def test_no_recent_demand_returns_zero_bonus():
    assert compute_lane_demand_score(_count_session([]), "delhi:jaipur") == 0


def test_small_recent_demand_returns_small_bonus():
    loads = [
        _load_request("load-1", from_city="Delhi", to_city="Jaipur"),
        _load_request("load-2", from_city="Jaipur", to_city="Delhi"),
    ]

    assert compute_lane_demand_score(_count_session(loads), "delhi:jaipur") == 4


def test_large_recent_demand_returns_capped_bonus():
    loads = [
        _load_request(f"load-{idx}", from_city="Delhi", to_city="Jaipur")
        for idx in range(12)
    ]

    assert compute_lane_demand_score(_count_session(loads), "delhi:jaipur") == 16


def test_vehicle_specific_demand_scoring():
    loads = [
        _load_request("load-1", vehicle_type="medium"),
        _load_request("load-2", vehicle_type="medium"),
        _load_request("load-3", vehicle_type="trailer"),
        _load_request("load-4", from_city="Delhi", to_city="Mumbai", vehicle_type="medium"),
    ]

    assert compute_lane_demand_score(_count_session(loads), "delhi:jaipur", vehicle_type="medium") == 4
    assert compute_lane_demand_score(_count_session(loads), "delhi:jaipur", vehicle_type="trailer") == 2


def test_lane_demand_vehicle_fallback_when_missing():
    loads = [
        _load_request("load-1", vehicle_type=None),
        _load_request("load-2", vehicle_type=None),
        _load_request("load-3", vehicle_type=None),
    ]

    assert compute_lane_demand_score(_count_session(loads), "delhi:jaipur", vehicle_type="medium") == 6


def test_demand_score_determinism_per_pass():
    session = _count_session([_load_request("load-1", vehicle_type="medium")])
    score_context = {}

    score_a = compute_lane_demand_score(
        session,
        "delhi:jaipur",
        vehicle_type="medium",
        score_context=score_context,
    )
    score_b = compute_lane_demand_score(
        session,
        "delhi:jaipur",
        vehicle_type="medium",
        score_context=score_context,
    )

    assert score_a == 2
    assert score_b == 2
    assert session.query.call_count == 1


def test_demand_bonus_affects_ranking_order():
    owner = _owner()
    load = _load_request("load-rank")
    low_demand_listing = _listing("listing-a", canonical_lane_key="delhi:mumbai")
    high_demand_listing = _listing("listing-b", canonical_lane_key="delhi:jaipur")
    session = _ranking_session(["match-a", "match-b"])

    with patch(
        "app.services.matching_service.compute_lane_demand_score",
        side_effect=lambda _session, lane_key, **_kwargs: 10 if lane_key == "delhi:jaipur" else 0,
    ), patch(
        "app.services.matching_service.compute_lane_liquidity_score",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_supply_demand_ratio_score",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_operator_reliability_bonus",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_lane_activity_freshness_bonus",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_lane_specific_reliability_bonus",
        return_value=0,
    ):
        ranked = rank_matches(
            load,
            [(low_demand_listing, owner), (high_demand_listing, owner)],
            session,
        )

    assert [result["match_id"] for result in ranked] == ["match-b", "match-a"]
