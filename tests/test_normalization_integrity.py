import pytest
from app.services.logistics_data import normalize_hub_name

def test_normalization_integrity():
    """
    Ensures zero lane-key fragmentation by verifying that common operator
    shorthands and government aliases all map to the canonical Logistics Hubs.
    """
    # Transport operator shorthands
    assert normalize_hub_name("blr") == "bangalore"
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
