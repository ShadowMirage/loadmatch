import pytest
from unittest.mock import MagicMock
from app.contracts.enums import Intent
from app.services.extraction_engine import ExtractionEngine, ExtractionResult

@pytest.mark.anyio
async def test_extraction_greeting_hello():
    ee = ExtractionEngine("test-trace")
    result = await ee.extract("hello", MagicMock(), {})
    assert result.intent == Intent.GREETING
    assert result.confidence == 1.0
    assert result.source == "RULE"

@pytest.mark.anyio
async def test_extraction_greeting_hello_ji():
    ee = ExtractionEngine("test-trace")
    result = await ee.extract("hello ji", MagicMock(), {})
    assert result.intent == Intent.GREETING
    assert result.confidence == 1.0
    assert result.source == "RULE"

@pytest.mark.anyio
async def test_extraction_greeting_start():
    ee = ExtractionEngine("test-trace")
    result = await ee.extract("start", MagicMock(), {})
    assert result.intent == Intent.GREETING
    assert result.confidence == 1.0

@pytest.mark.anyio
async def test_extraction_greeting_menu():
    ee = ExtractionEngine("test-trace")
    # The extraction engine should map "menu" to Intent.GREETING (as it is one of GREETING_WORDS)
    result = await ee.extract("menu", MagicMock(), {})
    assert result.intent == Intent.GREETING
