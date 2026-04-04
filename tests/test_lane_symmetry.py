import pytest
from app.contracts.enums import Intent
from app.services.intent_resolver import IntentResolver
from app.services.extraction_engine import ExtractionResult

@pytest.fixture
def resolver():
    return IntentResolver()

def test_lane_key_symmetry_delhi_jaipur(resolver):
    # delhi to jaipur
    extr1 = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr1, None, "IDLE", message_text="delhi to jaipur")
    
    # jaipur to delhi
    extr2 = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t2")
    resolver.resolve(extr2, None, "IDLE", message_text="jaipur to delhi")
    
    assert extr1.data["lane_key"] == "delhi:jaipur"
    assert extr2.data["lane_key"] == "delhi:jaipur"
    assert extr1.data["lane_key"] == extr2.data["lane_key"]
    
    # Directional keys must differ
    assert extr1.data["directional_lane_key"] == "delhi->jaipur"
    assert extr2.data["directional_lane_key"] == "jaipur->delhi"
    assert extr1.data["directional_lane_key"] != extr2.data["directional_lane_key"]

def test_lane_key_symmetry_aliases(resolver):
    # blr to delhi
    extr1 = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr1, None, "IDLE", message_text="blr to delhi")
    
    # delhi to bangalore
    extr2 = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t2")
    resolver.resolve(extr2, None, "IDLE", message_text="delhi to bangalore")
    
    assert extr1.data["lane_key"] == "bangalore:delhi"
    assert extr2.data["lane_key"] == "bangalore:delhi"
    assert extr1.data["lane_key"] == extr2.data["lane_key"]
