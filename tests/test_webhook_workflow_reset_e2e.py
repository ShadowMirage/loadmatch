import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.enums import Intent
from app.contracts.extraction import ExtractionResult
from app.routers.webhook import _phase2_atomic_dispatch
from app.services.state_machine_service import StateMachineService


def test_cancel_then_restart_same_flow_starts_clean():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="IDLE", updated_at=None)
    idempotency = MagicMock()
    idempotency.start.side_effect = [
        SimpleNamespace(id="pm-1"),
        SimpleNamespace(id="pm-2"),
        SimpleNamespace(id="pm-3"),
        SimpleNamespace(id="pm-4"),
    ]
    state_machine = StateMachineService()
    session_store = {
        "current_workflow": None,
        "session_data": {},
    }

    def fake_get_or_create_session(_db, _phone, _user_id):
        return SimpleNamespace(
            current_workflow=session_store["current_workflow"],
            session_data=session_store["session_data"],
        )

    def fake_set_session_data(_db, _phone, _user_id, data):
        session_store["session_data"] = {**session_store["session_data"], **(data or {})}

    def fake_get_session_data(_db, _phone, _user_id, create=False):
        return dict(session_store["session_data"])

    def fake_update_session(_db, _phone, updates, commit=True):
        if "current_workflow" in updates:
            session_store["current_workflow"] = updates["current_workflow"]

    def fake_clear_session(_db, _phone):
        session_store["current_workflow"] = None
        session_store["session_data"] = {}

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_or_create_session", side_effect=fake_get_or_create_session), \
             patch("app.routers.webhook.set_session_data", side_effect=fake_set_session_data), \
             patch("app.routers.webhook.update_session", side_effect=fake_update_session), \
             patch("app.routers.webhook.clear_session", side_effect=fake_clear_session), \
             patch("app.services.dispatcher_service.get_session_data", side_effect=fake_get_session_data), \
             patch("app.services.dispatcher_service.set_session_data", side_effect=fake_set_session_data), \
             patch("app.services.dispatcher_service.clear_session", side_effect=fake_clear_session):
            first_response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.1",
                db=db,
                trace_id="trace-1",
                intent=Intent.POST_TRUCK,
                payload={"data": {}},
                extraction=ExtractionResult(
                    intent=Intent.POST_TRUCK,
                    data={},
                    confidence=1.0,
                    source="TEST",
                    trace_id="trace-1",
                ),
                idempotency=idempotency,
                state_machine=state_machine,
            )

            second_response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.2",
                db=db,
                trace_id="trace-2",
                intent=Intent.POST_TRUCK,
                payload={
                    "data": {
                        "current_city": "jaipur",
                        "to_city": "gwalior",
                        "capacity_kg": 7000,
                        "departure_date": "2026-03-29",
                    }
                },
                extraction=ExtractionResult(
                    intent=Intent.POST_TRUCK,
                    data={
                        "current_city": "jaipur",
                        "to_city": "gwalior",
                        "capacity_kg": 7000,
                        "departure_date": "2026-03-29",
                    },
                    confidence=1.0,
                    source="TEST",
                    trace_id="trace-2",
                ),
                idempotency=idempotency,
                state_machine=state_machine,
            )

            cancel_response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.3",
                db=db,
                trace_id="trace-3",
                intent=Intent.CANCEL,
                payload={"action": "CANCEL"},
                extraction=ExtractionResult(
                    intent=Intent.CANCEL,
                    data={},
                    confidence=1.0,
                    source="TEST",
                    trace_id="trace-3",
                ),
                idempotency=idempotency,
                state_machine=state_machine,
            )

            restart_response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.4",
                db=db,
                trace_id="trace-4",
                intent=Intent.POST_TRUCK,
                payload={"data": {}},
                extraction=ExtractionResult(
                    intent=Intent.POST_TRUCK,
                    data={},
                    confidence=1.0,
                    source="TEST",
                    trace_id="trace-4",
                ),
                idempotency=idempotency,
                state_machine=state_machine,
            )

            return first_response, second_response, cancel_response, restart_response

    first_response, second_response, cancel_response, restart_response = asyncio.run(run())

    assert "Please provide:" in first_response.text and "1. Pickup city" in first_response.text
    assert "Confirming your truck availability" in second_response.text
    assert "cancelled" in cancel_response.text.lower()
    assert "Please provide:" in restart_response.text and "1. Pickup city" in restart_response.text
