from unittest.mock import MagicMock

from app.services.matching_service import compute_supply_demand_ratio_score


def _score_context_for_counts(
    lane: str,
    *,
    vehicle_type: str | None,
    demand_count: int,
    supply_count: int,
) -> dict:
    key = (lane, vehicle_type or "__lane_level__")
    return {
        "lane_demand_count": {key: demand_count},
        "lane_liquidity_count": {key: supply_count},
    }


def test_supply_demand_ratio_boosts_underserved_lane():
    score_context = _score_context_for_counts(
        "delhi:jaipur",
        vehicle_type="medium",
        demand_count=12,
        supply_count=2,
    )

    assert (
        compute_supply_demand_ratio_score(
            MagicMock(),
            "delhi:jaipur",
            vehicle_type="medium",
            score_context=score_context,
        )
        == 20
    )


def test_supply_demand_ratio_penalizes_oversupplied_lane():
    score_context = _score_context_for_counts(
        "delhi:jaipur",
        vehicle_type="medium",
        demand_count=2,
        supply_count=20,
    )

    assert (
        compute_supply_demand_ratio_score(
            MagicMock(),
            "delhi:jaipur",
            vehicle_type="medium",
            score_context=score_context,
        )
        == 0
    )


def test_ratio_score_caps_at_max():
    score_context = _score_context_for_counts(
        "delhi:jaipur",
        vehicle_type=None,
        demand_count=100,
        supply_count=1,
    )

    assert compute_supply_demand_ratio_score(MagicMock(), "delhi:jaipur", score_context=score_context) == 20


def test_ratio_score_deterministic():
    session = MagicMock()
    score_context = _score_context_for_counts(
        "delhi:jaipur",
        vehicle_type="trailer",
        demand_count=6,
        supply_count=3,
    )

    score_a = compute_supply_demand_ratio_score(
        session,
        "delhi:jaipur",
        vehicle_type="trailer",
        score_context=score_context,
    )
    score_b = compute_supply_demand_ratio_score(
        session,
        "delhi:jaipur",
        vehicle_type="trailer",
        score_context=score_context,
    )

    assert score_a == 10
    assert score_b == 10
    assert session.query.call_count == 0
