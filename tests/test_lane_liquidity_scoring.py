from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.enums import KycFlowState
from app.services.matching_service import (
    compute_lane_liquidity_score,
    rank_matches,
    score_match,
)


def _owner(owner_id: str = "owner-1", rating: float = 4.8):
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
    )


def _listing(
    listing_id: str,
    *,
    from_city: str = "Delhi",
    to_city: str = "Jaipur",
    canonical_lane_key: str | None = "delhi:jaipur",
):
    return SimpleNamespace(
        id=listing_id,
        owner_id=f"owner-{listing_id}",
        from_city=from_city,
        to_city=to_city,
        canonical_lane_key=canonical_lane_key,
        departure_date=date.today(),
        available_capacity_kg=5000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=datetime.now(timezone.utc),
    )


def _count_session(listing_count: int):
    query = MagicMock()
    query.filter.return_value = query
    query.count.return_value = listing_count
    session = MagicMock()
    session.query.return_value = query
    return session


def _sequential_count_session(counts: list[int]):
    remaining = list(counts)
    session = MagicMock()

    def query_side_effect(_model):
        query = MagicMock()
        query.filter.return_value = query

        def next_count():
            if not remaining:
                raise AssertionError("Unexpected extra count query")
            return remaining.pop(0)

        query.count.side_effect = next_count
        return query

    session.query.side_effect = query_side_effect
    return session


def _ranking_session(liquidity_counts: dict[str, int]):
    match_query = MagicMock()
    match_query.filter.return_value = match_query
    match_query.first.return_value = None

    def query_side_effect(model):
        model_name = getattr(model, "__name__", "")
        if model_name == "Match":
            return match_query

        query = MagicMock()
        query.filter.return_value = query
        query.count.side_effect = lambda: liquidity_counts.get(
            query.filter.call_args_list[0].args[0].right.value, 0
        )
        return query

    session = MagicMock()
    session.query.side_effect = query_side_effect
    session.flush.return_value = None
    return session


def test_liquidity_score_increases_with_listing_count():
    load = _load()
    listing = _listing("listing-1")
    owner = _owner()

    base_score = score_match(load, listing, owner)
    with patch("app.services.matching_service.compute_operator_reliability_bonus", return_value=0), patch(
        "app.services.matching_service.compute_lane_demand_score", return_value=0
    ), patch(
        "app.services.matching_service.compute_supply_demand_ratio_score", return_value=0
    ), patch(
        "app.services.matching_service.compute_lane_activity_freshness_bonus", return_value=0
    ), patch(
        "app.services.matching_service.compute_lane_specific_reliability_bonus", return_value=0
    ):
        boosted_score = score_match(load, listing, owner, _count_session(3))

    assert compute_lane_liquidity_score(_count_session(3), "delhi:jaipur") == 6
    assert boosted_score == min(base_score + 6, 100)


def test_liquidity_score_caps_at_max():
    assert compute_lane_liquidity_score(_count_session(25), "delhi:jaipur") == 20


def test_liquidity_score_zero_when_no_recent_supply():
    assert compute_lane_liquidity_score(_count_session(0), "delhi:jaipur") == 0


def test_vehicle_specific_liquidity_scoring():
    assert (
        compute_lane_liquidity_score(
            _sequential_count_session([3]),
            "delhi:jaipur",
            vehicle_type="medium",
        )
        == 6
    )
    assert (
        compute_lane_liquidity_score(
            _sequential_count_session([1]),
            "delhi:jaipur",
            vehicle_type="trailer",
        )
        == 2
    )


def test_lane_liquidity_vehicle_fallback_when_missing():
    # typed count = 0, typed-lane count = 0, lane-level count = 4 -> fallback to lane-level
    assert (
        compute_lane_liquidity_score(
            _sequential_count_session([0, 0, 4]),
            "delhi:jaipur",
            vehicle_type="medium",
        )
        == 8
    )


def test_liquidity_score_determinism_per_pass():
    session = _sequential_count_session([2])
    score_context = {}

    score_a = compute_lane_liquidity_score(
        session,
        "delhi:jaipur",
        vehicle_type="medium",
        score_context=score_context,
    )
    score_b = compute_lane_liquidity_score(
        session,
        "delhi:jaipur",
        vehicle_type="medium",
        score_context=score_context,
    )

    assert score_a == 4
    assert score_b == 4
    assert session.query.call_count == 1


def test_matching_order_improves_deterministically_with_liquidity():
    load = _load()
    high_liquidity_listing = _listing("listing-a", canonical_lane_key="delhi:jaipur")
    low_liquidity_listing = _listing("listing-b", canonical_lane_key="delhi:mumbai")
    owner = _owner()
    session = _ranking_session({"delhi:jaipur": 6, "delhi:mumbai": 0})

    with patch(
        "app.services.matching_service.compute_lane_liquidity_score",
        side_effect=lambda _session, lane_key, **_kwargs: 12 if lane_key == "delhi:jaipur" else 0,
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
        "app.services.matching_service.compute_lane_activity_freshness_bonus",
        return_value=0,
    ), patch(
        "app.services.matching_service.compute_lane_specific_reliability_bonus",
        return_value=0,
    ):
        ranked = rank_matches(
            load,
            [(low_liquidity_listing, owner), (high_liquidity_listing, owner)],
            session,
        )

    assert ranked[0]["from_city"] == "Delhi"
    assert ranked[0]["to_city"] == "Jaipur"
