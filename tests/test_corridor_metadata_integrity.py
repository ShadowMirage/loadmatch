import pytest
from app.contracts.enums import Intent
from app.services.intent_resolver import IntentResolver
from app.services.extraction_engine import ExtractionResult
from app.services.logistics_data import CorridorSource

@pytest.fixture
def resolver():
    return IntentResolver()

def test_bangalore_delhi_corridor_metadata(resolver):
    extraction = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="trace-corridor")
    resolver.resolve(extraction, None, "IDLE", message_text="blr to delhi")
    
    assert extraction.data["from_city"] == "bangalore"
    assert extraction.data["to_city"] == "delhi"
    assert extraction.data["lane_key"] == "bangalore:delhi"
    assert extraction.data["directional_lane_key"] == "bangalore->delhi"
    assert extraction.data["corridor_detected"] is True
    assert extraction.data["confidence_source"] == "corridor_detection"
    
    # Critical invariant: sorted(lane_key.split(":")) must equal sorted([from_city, to_city])
    lane_parts = sorted(extraction.data["lane_key"].split(":"))
    city_parts = sorted([extraction.data["from_city"], extraction.data["to_city"]])
    assert lane_parts == city_parts

def test_ncr_jaipur_alias_metadata(resolver):
    extraction = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="trace-alias")
    resolver.resolve(extraction, None, "IDLE", message_text="ncr to jaipur")
    
    assert extraction.data["from_city"] == "delhi"
    assert extraction.data["to_city"] == "jaipur"
    assert extraction.data["corridor_source"] == CorridorSource.ALIAS_PAIR
    assert extraction.data["corridor_detected"] is True
