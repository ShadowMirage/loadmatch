import pytest
from app.legacy.chatbot_runtime.bot_tools import (
    _backhaul_city_candidates,
    _is_backhaul_origin_match,
)
from app.services.logistics_data import normalize_hub_name

def test_normalization_integrity():
    """
    Ensures zero lane-key fragmentation by verifying that common operator
    shorthands and government aliases all map to the canonical Logistics Hubs.
    """
    # Transport operator shorthands
    assert normalize_hub_name("blr") == "bangalore"
    assert normalize_hub_name("banglore") == "bangalore"
    assert normalize_hub_name("mum") == "mumbai"
    assert normalize_hub_name("ncr") == "delhi"
    assert normalize_hub_name("amd") == "ahmedabad"
    
    # Common misspellings / alternate names
    assert normalize_hub_name("bengaluru") == "bangalore"
    assert normalize_hub_name("baroda") == "vadodara"
    assert normalize_hub_name("bombay") == "mumbai"
    assert normalize_hub_name("gurugram") == "gurgaon"
    
    # Direct hub matches
    assert normalize_hub_name("delhi") == "delhi"
    assert normalize_hub_name("bangalore") == "bangalore"

def test_normalization_invalid():
    """Verifies that non-logistics cities fall through securely."""
    assert normalize_hub_name("unknown_village_399") is None


def test_legacy_backhaul_normalization_matches_mixed_origin_records():
    candidates = _backhaul_city_candidates("Delhi")

    assert "delhi" in candidates
    assert "ncr" in candidates
    assert _is_backhaul_origin_match("NCR", "Delhi") is True
