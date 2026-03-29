import pytest
from app.contracts.enums import Intent
from app.services.intent_resolver import IntentResolver
from app.services.extraction_engine import ExtractionResult
from app.services.logistics_data import CorridorSource, LaneClass

@pytest.fixture
def resolver():
    return IntentResolver()

def test_v3_metadata_structure(resolver):
    # Case 1: Standard city pair
    extraction = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="test-trace")
    intent = resolver.resolve(extraction, None, "IDLE", message_text="delhi to jaipur")
    
    assert intent == Intent.CREATE_LOAD
    assert extraction.confidence == 1.0
    assert extraction.data["corridor_detected"] is True
    assert extraction.data["corridor_source"] == CorridorSource.CITY_PAIR
    assert extraction.data["confidence_source"] == "corridor_detection"
    assert extraction.data["resolver_version"] == "v3_corridor_payload_acceleration"
    assert extraction.data["lane_detected_via"] == "separator_window"

def test_lane_key_symmetry_reverse_order(resolver):
    # Case 2: Verification that lane_key is canonical (sorted)
    # A -> B
    extr1 = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr1, None, "IDLE", message_text="bangalore to delhi")
    
    # B -> A
    extr2 = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t2")
    resolver.resolve(extr2, None, "IDLE", message_text="delhi to bangalore")
    
    # Symbols: sorted(bangalore, delhi) = bangalore, delhi
    assert extr1.data["lane_key"] == "bangalore:delhi"
    assert extr2.data["lane_key"] == "bangalore:delhi"
    assert extr1.data["lane_cities"] == ["bangalore", "delhi"]

def test_directional_lane_key(resolver):
    # Case 3: Verification of flow semantics
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="jaipur se delhi")
    
    assert extr.data["directional_lane_key"] == "jaipur->delhi"
    assert extr.data["from_city"] == "jaipur"
    assert extr.data["to_city"] == "delhi"

def test_alias_to_payload_normalization(resolver):
    # Case 4: End-to-end normalization (ncr -> delhi)
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="ncr to jaipur")
    
    assert extr.data["from_city"] == "delhi"
    assert extr.data["corridor_source"] == CorridorSource.ALIAS_PAIR

def test_lane_key_alias_collapse(resolver):
    # Case 4b: Alias collapse for lane key stability
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="blr to delhi")
    
    assert extr.data["lane_key"] == "bangalore:delhi"
    assert extr.data["from_city"] == "bangalore"
    assert extr.data["to_city"] == "delhi"

def test_industrial_zone_metadata(resolver):
    # Case 5: GIDC zones
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="ankleshwar gidc to vapi")
    
    assert extr.data["from_city"] == "ankleshwar"
    assert extr.data["corridor_source"] == CorridorSource.INDUSTRIAL_ZONE_PAIR

def test_adjacency_metadata(resolver):
    # Case 6: Shorthand (delhi jaipur)
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="delhi jaipur")
    
    assert extr.data["corridor_source"] == CorridorSource.ADJACENT_CITY_PAIR
    assert extr.data["lane_detected_via"] == "adjacent_tokens"

def test_truck_heuristic_boost(resolver):
    # Case 7: Basic keyword signals (non-corridor)
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.5, source="TEST", trace_id="t1")
    intent = resolver.resolve(extr, None, "IDLE", message_text="truck available")
    
    assert intent == Intent.POST_TRUCK
    assert extr.confidence == 0.75 # 0.5 + 0.25 (capped 0.92)

def test_strict_route_validation(resolver):
    # Case 8: Prevention of false positives
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    intent = resolver.resolve(extr, None, "IDLE", message_text="go to delhi")
    
    assert intent == Intent.UNKNOWN
    assert "corridor_detected" not in extr.data

def test_lane_class_tier1_spine(resolver):
    """Both Tier-1 metros → tier1_spine"""
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="delhi to bangalore")
    assert extr.data["lane_class"] == LaneClass.TIER1_SPINE

def test_lane_class_industrial_cluster(resolver):
    """Gujarat spine city → industrial_cluster"""
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="ankleshwar to surat")
    assert extr.data["lane_class"] == LaneClass.INDUSTRIAL_CLUSTER

def test_reverse_directional_lane_key(resolver):
    """Reverse fingerprint must be the mirror of directional_lane_key"""
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="jaipur to delhi")
    assert extr.data["directional_lane_key"] == "jaipur->delhi"
    assert extr.data["reverse_directional_lane_key"] == "delhi->jaipur"

def test_corridor_source_is_enum(resolver):
    """corridor_source must be a typed CorridorSource enum, not a raw string"""
    extr = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    resolver.resolve(extr, None, "IDLE", message_text="delhi to jaipur")
    assert isinstance(extr.data["corridor_source"], CorridorSource)
    assert extr.data["corridor_source"] == CorridorSource.CITY_PAIR
