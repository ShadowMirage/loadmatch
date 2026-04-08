from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.enums import KycFlowState
from app.models.match import Match
from app.services.matching_service import rank_matches, score_match


def _owner():
    return SimpleNamespace(
        id="owner-1",
        name="Owner",
        phone="919999999999",
        rating=4.8,
        completed_trips=10,
        kyc_flow_state=KycFlowState.verified,
    )


def _load(*, corridor_source="city_pair"):
    return SimpleNamespace(
        id="load-1",
        shipper_id="shipper-1",
        from_city="Delhi",
        to_city="Jaipur",
        pickup_date=date.today(),
        weight_kg=5000,
        category=None,
        corridor_source=corridor_source,
    )


def _listing(
    listing_id: str,
    *,
    from_city: str = "Delhi",
    to_city: str = "Jaipur",
    canonical_lane_key: str = "delhi:jaipur",
    created_at: datetime | None = None,
):
    return SimpleNamespace(
        id=listing_id,
        owner_id="owner-1",
        from_city=from_city,
        to_city=to_city,
        canonical_lane_key=canonical_lane_key,
        departure_date=date.today(),
        available_capacity_kg=5000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=created_at or (datetime.now(timezone.utc) - timedelta(minutes=10)),
    )


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


def test_ranking_score_breakdown_logged():
    load = _load()
    listing = _listing("listing-1")
    owner = _owner()

    with patch("app.services.matching_service.compute_lane_liquidity_score", return_value=6), patch(
        "app.services.matching_service.compute_lane_demand_score", return_value=4
    ), patch(
        "app.services.matching_service.compute_supply_demand_ratio_score", return_value=2
    ), patch(
        "app.services.matching_service.compute_lane_activity_freshness_bonus", return_value=3
    ), patch(
        "app.services.matching_service.compute_operator_reliability_bonus", return_value=3
    ), patch(
        "app.services.matching_service.compute_lane_specific_reliability_bonus", return_value=2
    ), patch(
        "app.services.matching_service.logger.info"
    ) as mock_info:
        score = score_match(load, listing, owner, MagicMock())

    assert any(
        call.args
        and call.args[0] == "RANKING_SCORE_BREAKDOWN"
        and call.kwargs["extra"]["listing_id"] == "listing-1"
        and call.kwargs["extra"]["lane_key"] == "delhi:jaipur"
        and call.kwargs["extra"]["corridor_bonus"] == 10
        and call.kwargs["extra"]["liquidity_bonus"] == 6
        and call.kwargs["extra"]["lane_demand_bonus"] == 4
        and call.kwargs["extra"]["supply_demand_imbalance_bonus"] == 2
        and call.kwargs["extra"]["lane_activity_freshness_bonus"] == 3
        and call.kwargs["extra"]["recency_bonus"] == 10
        and call.kwargs["extra"]["operator_reliability_bonus"] == 3
        and call.kwargs["extra"]["lane_specific_reliability_bonus"] == 2
        and call.kwargs["extra"]["final_score"] == score
        for call in mock_info.call_args_list
    )


def test_match_order_decision_trace_logged():
    load = _load()
    owner = _owner()
    listing_a = _listing("listing-a")
    listing_b = _listing("listing-b", to_city="Mumbai", canonical_lane_key="delhi:mumbai")
    session = _ranking_session(["match-b", "match-a"])

    with patch("app.services.matching_service.score_match", side_effect=[70, 90]), patch(
        "app.services.matching_service.logger.info"
    ) as mock_info:
        ranked = rank_matches(load, [(listing_a, owner), (listing_b, owner)], session)

    assert [result["match_id"] for result in ranked] == ["match-a", "match-b"]
    assert any(
        call.args
        and call.args[0] == "MATCH_ORDER_DECISION_TRACE"
        and call.kwargs["extra"]
        == {
            "lane_key": "delhi:jaipur",
            "candidate_ids": ["match-a", "match-b"],
        }
        for call in mock_info.call_args_list
    )
