from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.enums import KycFlowState
from app.models.match import Match
from app.services.matching_service import rank_matches


def _owner():
    return SimpleNamespace(
        id="owner-1",
        name="Owner",
        phone="919999999999",
        rating=4.8,
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


def _listing(listing_id: str):
    return SimpleNamespace(
        id=listing_id,
        owner_id="owner-1",
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key="delhi:jaipur",
        departure_date=date.today() + timedelta(days=1),
        available_capacity_kg=5000,
        allowed_categories=None,
        price_per_kg=Decimal("2.0"),
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
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


def _score_side_effect(
    breakdown_by_listing_id: dict[str, dict[str, float]],
    *,
    score: int = 80,
):
    def _fake_score_match(*args, **kwargs):
        listing = args[1]
        score_context = kwargs.get("score_context")
        breakdown_bucket = score_context.setdefault("listing_breakdown", {})
        breakdown_bucket[str(listing.id)] = breakdown_by_listing_id[str(listing.id)]
        return score

    return _fake_score_match


def test_equal_score_stable_listing_id_order():
    load = _load()
    owner = _owner()
    listing_a = _listing("listing-a")
    listing_b = _listing("listing-b")
    session = _ranking_session(["match-b", "match-a"])

    breakdown = {
        "listing-a": {
            "vehicle_specific_score": 10,
            "lane_score": 10,
            "recency_score": 10,
            "operator_score": 10,
        },
        "listing-b": {
            "vehicle_specific_score": 10,
            "lane_score": 10,
            "recency_score": 10,
            "operator_score": 10,
        },
    }

    with patch(
        "app.services.matching_service.score_match",
        side_effect=_score_side_effect(breakdown),
    ):
        ranked = rank_matches(load, [(listing_b, owner), (listing_a, owner)], session)

    assert [result["match_id"] for result in ranked] == ["match-a", "match-b"]


def test_vehicle_specific_score_priority_over_lane_score():
    load = _load()
    owner = _owner()
    listing_high_lane = _listing("listing-high-lane")
    listing_high_vehicle = _listing("listing-high-vehicle")
    session = _ranking_session(["match-lane", "match-vehicle"])

    breakdown = {
        "listing-high-lane": {
            "vehicle_specific_score": 5,
            "lane_score": 20,
            "recency_score": 20,
            "operator_score": 20,
        },
        "listing-high-vehicle": {
            "vehicle_specific_score": 6,
            "lane_score": 0,
            "recency_score": 0,
            "operator_score": 0,
        },
    }

    with patch(
        "app.services.matching_service.score_match",
        side_effect=_score_side_effect(breakdown),
    ):
        ranked = rank_matches(
            load,
            [(listing_high_lane, owner), (listing_high_vehicle, owner)],
            session,
        )

    assert [result["match_id"] for result in ranked] == ["match-vehicle", "match-lane"]


def test_lane_score_priority_over_recency():
    load = _load()
    owner = _owner()
    listing_high_recency = _listing("listing-high-recency")
    listing_high_lane = _listing("listing-high-lane")
    session = _ranking_session(["match-recency", "match-lane"])

    breakdown = {
        "listing-high-recency": {
            "vehicle_specific_score": 5,
            "lane_score": 2,
            "recency_score": 9,
            "operator_score": 0,
        },
        "listing-high-lane": {
            "vehicle_specific_score": 5,
            "lane_score": 3,
            "recency_score": 0,
            "operator_score": 10,
        },
    }

    with patch(
        "app.services.matching_service.score_match",
        side_effect=_score_side_effect(breakdown),
    ):
        ranked = rank_matches(
            load,
            [(listing_high_recency, owner), (listing_high_lane, owner)],
            session,
        )

    assert [result["match_id"] for result in ranked] == ["match-lane", "match-recency"]


def test_recency_priority_over_operator_score():
    load = _load()
    owner = _owner()
    listing_high_operator = _listing("listing-high-operator")
    listing_high_recency = _listing("listing-high-recency")
    session = _ranking_session(["match-operator", "match-recency"])

    breakdown = {
        "listing-high-operator": {
            "vehicle_specific_score": 5,
            "lane_score": 5,
            "recency_score": 2,
            "operator_score": 9,
        },
        "listing-high-recency": {
            "vehicle_specific_score": 5,
            "lane_score": 5,
            "recency_score": 3,
            "operator_score": 0,
        },
    }

    with patch(
        "app.services.matching_service.score_match",
        side_effect=_score_side_effect(breakdown),
    ):
        ranked = rank_matches(
            load,
            [(listing_high_operator, owner), (listing_high_recency, owner)],
            session,
        )

    assert [result["match_id"] for result in ranked] == ["match-recency", "match-operator"]
