from datetime import date, timedelta
from types import SimpleNamespace

from app.models.enums import KycFlowState
from app.services.matching_service import corridor_bonus, score_match
from app.services.route_corridors import get_corridor, get_nearby_routes, is_in_corridor


def _owner(rating: float, verified: bool = True, completed_trips: int = 0):
    return SimpleNamespace(
        rating=rating,
        kyc_flow_state=KycFlowState.verified if verified else KycFlowState.not_started,
        completed_trips=completed_trips,
    )


def _load(from_city: str, to_city: str, pickup_date: date, weight_kg: int):
    return SimpleNamespace(
        from_city=from_city,
        to_city=to_city,
        pickup_date=pickup_date,
        weight_kg=weight_kg,
    )


def _listing(from_city: str, to_city: str, departure_date: date, capacity_kg: int):
    return SimpleNamespace(
        from_city=from_city,
        to_city=to_city,
        departure_date=departure_date,
        available_capacity_kg=capacity_kg,
    )


def test_score_match_rewards_exact_route_date_capacity_and_reputation():
    today = date.today()
    owner = _owner(rating=4.9, completed_trips=12)
    load = _load("Delhi", "Mumbai", today, 200)
    listing = _listing("Delhi", "Mumbai", today, 200)

    assert score_match(load, listing, owner) == 100


def test_score_match_drops_when_route_date_and_capacity_are_worse():
    today = date.today()
    strong_owner = _owner(rating=4.9, completed_trips=12)
    weaker_owner = _owner(rating=3.6, verified=False, completed_trips=0)

    ideal = score_match(
        _load("Delhi", "Mumbai", today, 200),
        _listing("Delhi", "Mumbai", today, 200),
        strong_owner,
    )
    weaker = score_match(
        _load("Delhi", "Mumbai", today, 200),
        _listing("Pune", "Mumbai", today + timedelta(days=2), 600),
        weaker_owner,
    )

    assert weaker < ideal


def test_score_match_normalizes_city_aliases_before_scoring():
    today = date.today()
    owner = _owner(rating=4.9, completed_trips=12)
    load = _load("New Delhi", "Mumbai", today, 200)
    listing = _listing("Delhi", "Bombay", today, 200)

    assert score_match(load, listing, owner) == 100


def test_corridor_lookup_and_membership_use_canonical_city_forms():
    corridor = get_corridor("Jaipur", "NEW DELHI")

    assert corridor is not None
    assert corridor[0] == "jaipur"
    assert corridor[-1] == "delhi"
    assert is_in_corridor("Jaipur", "Gurgaon", "Jaipur", "New Delhi") is True


def test_nearby_routes_return_display_ready_route_labels():
    assert ("Jaipur", "Delhi") in get_nearby_routes("Bhiwadi", "Delhi")


def test_corridor_bonus_preserves_alias_and_industrial_zone_provenance():
    assert corridor_bonus("ankleshwar gidc", "vapi") == 12
    assert corridor_bonus("ncr", "jaipur") == 8
    # Alias collapse survives scoring logic
    assert corridor_bonus("baroda", "surat") == corridor_bonus("vadodara", "surat")
