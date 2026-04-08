from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.models.enums import KycFlowState
from app.services.matching_service import corridor_rank_bonus, score_match


def _owner():
    return SimpleNamespace(
        rating=4.8,
        kyc_flow_state=KycFlowState.verified,
        completed_trips=10,
    )


def _load(corridor_source=None):
    return SimpleNamespace(
        from_city="Delhi",
        to_city="Jaipur",
        pickup_date=date.today(),
        weight_kg=5000,
        corridor_source=corridor_source,
    )


def _listing():
    return SimpleNamespace(
        from_city="Delhi",
        to_city="Jaipur",
        canonical_lane_key="delhi:jaipur",
        departure_date=date.today(),
        available_capacity_kg=5000,
    )


def test_industrial_zone_pair_bonus():
    assert corridor_rank_bonus("industrial_zone_pair") == 12


def test_city_pair_bonus():
    assert corridor_rank_bonus("city_pair") == 10


def test_adjacent_pair_bonus():
    assert corridor_rank_bonus("adjacent_city_pair") == 9


def test_alias_pair_bonus():
    assert corridor_rank_bonus("alias_pair") == 8


def test_missing_corridor_source_no_bonus():
    assert corridor_rank_bonus(None) == 0


def test_corridor_ranking_applied_marker_is_deterministic():
    owner = _owner()
    listing = _listing()
    load = _load("city_pair")

    with patch("app.services.matching_service.logger.info") as mock_info:
        score = score_match(load, listing, owner)

    assert score == 100
    assert any(
        call.args and call.args[0] == "CORRIDOR_RANKING_APPLIED"
        and call.kwargs["extra"] == {
            "lane_key": "delhi:jaipur",
            "corridor_source": "city_pair",
            "score_delta": 10,
        }
        for call in mock_info.call_args_list
    )
