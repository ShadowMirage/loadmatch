import pytest
from unittest.mock import MagicMock
from app.contracts.enums import Intent
from app.contracts.responses import Response as ContractResponse
from app.services.dispatcher_service import DispatcherService

@pytest.fixture
def db():
    return MagicMock()

@pytest.fixture
def dispatcher(db):
    return DispatcherService(db, "test_user_id", phone="919999999901")

def test_hello_idle(dispatcher):
    """Verifies that 'hello' in IDLE state returns the main menu."""
    resp = dispatcher.execute(Intent.GREETING, {}, current_workflow="IDLE")
    # Dispatcher should return the main menu response
    assert "Welcome to LoadMatch" in resp.text
    assert any(b.title == "Post Load" for b in resp.buttons)

def test_greeting_with_suffix(dispatcher):
    """Verifies that specialized greetings like 'hello ji' would be handled correctly (by extraction engine)."""
    # This test verifies the dispatcher's handling of the intent itself.
    # The actual suffix parsing happens in ExtractionEngine which we test separately or assume works from the unit code.
    resp = dispatcher.execute(Intent.GREETING, {}, current_workflow="IDLE")
    assert resp.text is not None
    assert "Welcome to LoadMatch" in resp.text

def test_hello_in_load_flow(dispatcher):
    """Verifies that 'hello' during an active LOAD_FLOW triggers the soft interrupt menu."""
    resp = dispatcher.execute(Intent.GREETING, {}, current_workflow="LOAD_FLOW")
    assert "You're currently in a workflow" in resp.text
    assert "1️⃣ Continue" in resp.text
    assert "2️⃣ Cancel" in resp.text
    assert "3️⃣ Main Menu" in resp.text

def test_hello_in_confirm_step(dispatcher):
    """Verifies that interrupts during confirmations use the restricted continue/cancel options."""
    resp = dispatcher.execute(Intent.GREETING, {}, current_workflow="LOAD_CONFIRM")
    assert "You are confirming an action" in resp.text
    assert "1️⃣ Continue" in resp.text
    assert "2️⃣ Cancel" in resp.text
    assert "Main Menu" not in resp.text # Should be confirmation-safe

def test_unknown_intent_interrupt(dispatcher):
    """Verifies that UNKNOWN intents also trigger the soft interrupt menu when in a workflow."""
    resp = dispatcher.execute(Intent.UNKNOWN, {}, current_workflow="LOAD_FLOW")
    assert "You're currently in a workflow" in resp.text

def test_menu_intent_interrupt(dispatcher):
    """Verifies that explicit MENU intent also triggers the soft interrupt menu when in a workflow."""
    resp = dispatcher.execute(Intent.MENU, {}, current_workflow="LOAD_FLOW")
    assert "You're currently in a workflow" in resp.text
