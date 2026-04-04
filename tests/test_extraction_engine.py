import pytest
import asyncio
from unittest.mock import patch, MagicMock
from app.services.extraction_engine import ExtractionEngine

@pytest.fixture
def extraction_engine():
    return ExtractionEngine("test-trace")

def test_extraction_confidence_cap(extraction_engine):
    """
    Ensures that legacy regex extraction can never exceed 0.50 confidence.
    This is critical because IntentResolver requires 0.60 to trigger its
    own high-fidelity heuristics. If ExtractionEngine exceeds 0.50, it
    would silently bypass the platform's routing pipeline.
    """
    
    # Force regex fallback by mocking out LLM requirements
    with patch.object(extraction_engine, '_should_use_llm', return_value=False):
        
        # Act
        # Delhi to Jaipur is a classic route that used to score 0.70 via legacy regex
        result = asyncio.run(extraction_engine.extract("delhi to jaipur 200kg", user=None, session_data={}))
        
        # Assert
        assert result.confidence <= 0.50
        assert result.source == "REGEX"
        assert result.data["confidence_source"] == "regex"


def test_llm_extraction_tags_structured_confidence_source(extraction_engine):
    ai_result = {
        "action": "confirm_load_request",
        "confidence": 0.95,
        "data": {
            "from_city": "delhi",
            "to_city": "jaipur",
            "weight_kg": 5000,
        },
    }

    with patch.object(extraction_engine, "_should_use_llm", return_value=True), \
         patch("app.services.extraction_engine.extract_with_context", return_value=ai_result):
        result = asyncio.run(extraction_engine.extract("delhi to jaipur 5 ton", user=None, session_data={}))

    assert result.source == "LLM"
    assert result.data["confidence_source"] == "llm_structured"
