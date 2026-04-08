import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.enums import Intent
from app.contracts.extraction import ExtractionResult
from app.contracts.responses import Response
from app.routers.webhook import _phase2_atomic_dispatch


def test_phase2_persists_ranking_context_timestamp_for_replay():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="LOAD_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="LOAD_FLOW", error_message=None)
    idempotency = MagicMock()
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CREATE_LOAD,
        data={"from_city": "delhi", "to_city": "jaipur"},
        confidence=1.0,
        source="TEST",
        trace_id="trace-lock",
    )
    captured = {}

    def start_side_effect(*args, **kwargs):
        captured["request_payload"] = args[2]
        return SimpleNamespace(id="pm-lock")

    def fake_execute(self, intent, payload, current_workflow=None):
        captured["dispatcher_payload"] = payload
        return Response(text="ok")

    idempotency.start.side_effect = start_side_effect

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_or_create_session", return_value=None), \
             patch("app.routers.webhook.set_session_data", return_value=None), \
             patch("app.routers.webhook.update_session", return_value=None), \
             patch("app.routers.webhook.DispatcherService.execute", new=fake_execute):
            return await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.lock",
                db=db,
                trace_id="trace-lock",
                intent=Intent.CREATE_LOAD,
                payload={"from_city": "delhi", "to_city": "jaipur"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )

    response, idem_key, msg_id = asyncio.run(run())

    request_payload = captured["request_payload"]
    timestamp = request_payload["ranking_context_timestamp"]
    assert timestamp
    assert request_payload["extraction_data"]["ranking_context_timestamp"] == timestamp
    assert request_payload["payload"]["ranking_context_timestamp"] == timestamp
    assert captured["dispatcher_payload"]["ranking_context_timestamp"] == timestamp
    assert response.text == "ok"
    assert idem_key == "user-123:CREATE_LOAD:wamid.lock:LOAD_FLOW"
    assert msg_id == "pm-lock"
