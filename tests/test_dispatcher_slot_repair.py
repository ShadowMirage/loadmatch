import pytest
from datetime import datetime, date
from unittest.mock import MagicMock
from app.services.dispatcher_service import DispatcherService
from app.contracts.enums import Intent

@pytest.fixture
def mock_db():
    return MagicMock()

@pytest.fixture
def relative_base():
    # April 13, 2026 baseline
    return datetime(2026, 4, 13, 10, 0, 0)

def test_dispatcher_slot_repair_invalid_to_valid(mock_db, relative_base):
    """
    Scenario:
    Turn 1: User sends "Alwat to agra" -> session contains from_city="alwat" (Unknown).
    Turn 2: User sends "Alwar" -> extraction contains from_city="alwar" (Valid).
    Result: from_city should be promoted to "alwar".
    """
    dispatcher = DispatcherService(mock_db, user_id="U1", relative_base=relative_base, phone="919999999999")
    
    # Mock Turn 1 state: Alwat is in session
    # (Actually we mock get_session_data to return the stale state)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.dispatcher_service.get_session_data", lambda *args, **kwargs: {"from_city": "alwat", "to_city": "agra"})
        
        # Turn 2 extraction: "Alwar"
        extraction_data = {"from_city": "alwar"}
        
        # We need to wrap extraction_data in a mock payload object or just pass it as a dict
        payload = {"data": extraction_data}
        
        merged = dispatcher._collect_payload_data(payload)
        
        # Verify Alwar (valid) won over Alwat (invalid)
        assert merged["from_city"] == "alwar"
        assert merged["to_city"] == "agra"

def test_dispatcher_date_hallucination_anchoring(mock_db, relative_base):
    """
    Scenario:
    User types "17 April".
    LLM returns "17-04-2023" (hallucinated year).
    Baseline is 2026.
    Result: coerced date must be 17-04-2026.
    """
    dispatcher = DispatcherService(mock_db, user_id="U1", relative_base=relative_base)
    
    # Hallucinated string from LLM
    hallucinated_val = "17-04-2023"
    coerced = dispatcher._coerce_date(hallucinated_val)
    
    assert coerced.year == 2026
    assert coerced.month == 4
    assert coerced.day == 17

def test_dispatcher_date_object_anchoring(mock_db, relative_base):
    """
    Scenario:
    Pydantic parsed 2023-04-17 into a date object.
    Baseline is 2026.
    Result: coerced date must still be 17-04-2026.
    """
    dispatcher = DispatcherService(mock_db, user_id="U1", relative_base=relative_base)
    
    # Hallucinated date object
    hallucinated_date = date(2023, 4, 17)
    coerced = dispatcher._coerce_date(hallucinated_date)
    
    assert coerced.year == 2026
    assert coerced.month == 4
    assert coerced.day == 17

def test_validity_aware_precedence(mock_db, relative_base):
    """
    Precedence: valid(payload) > valid(extraction) > valid(session).
    """
    dispatcher = DispatcherService(mock_db, user_id="U1", relative_base=relative_base)
    
    payload_val = "Unknown"  # Invalid
    extraction_val = "Jaipur" # Valid
    session_val = "Delhi"    # Valid
    
    # Precedence Rule: valid(session) protects against valid(extraction)
    # to avoid aggressive hallucinations from overwriting ground truth.
    selected = dispatcher._select_valid_slot("from_city", payload_val, extraction_val, session_val)
    assert selected == "Delhi"

def test_dispatcher_slot_valid_to_valid_preservation(mock_db, relative_base):
    """
    Scenario:
    Turn 1: Jaipur (Valid) is in session.
    Turn 2: Alwar (Valid) arrived via extraction (not payload/button).
    Expected: Jaipur is RETAINED to prevent noisy LLM hallucinations from overwriting valid ground truth.
    """
    dispatcher = DispatcherService(mock_db, user_id="U1", relative_base=relative_base)
    
    payload_val = None
    extraction_val = "Alwar"
    session_val = "Jaipur"
    
    selected = dispatcher._select_valid_slot("from_city", payload_val, extraction_val, session_val)
    assert selected == "Jaipur"

def test_dispatcher_slot_payload_overwrites_all(mock_db, relative_base):
    """
    Scenario:
    Jaipur (Valid) is in session.
    Alwar (Valid) arrived via PAYLOAD (e.g. Button click or explicit correction ID).
    Expected: Alwar WINS because payload represents explicit user intent.
    """
    dispatcher = DispatcherService(mock_db, user_id="U1", relative_base=relative_base)
    
    payload_val = "Alwar"
    extraction_val = "Delhi"
    session_val = "Jaipur"
    
    selected = dispatcher._select_valid_slot("from_city", payload_val, extraction_val, session_val)
    assert selected == "Alwar"
