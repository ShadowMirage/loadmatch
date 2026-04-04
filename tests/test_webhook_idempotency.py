import asyncio
import pytest
from unittest.mock import MagicMock, patch
from app.contracts.enums import Intent
from app.services.extraction_engine import ExtractionResult
from app.routers.webhook import _phase2_atomic_dispatch
from app.contracts.responses import Response

class _FakeDB:
    def __init__(self):
        self.added = []
    def add(self, obj):
        self.added.append(obj)
    def commit(self):
        pass
    def rollback(self):
        pass
    def flush(self):
        pass

@pytest.mark.asyncio
async def test_simulate_webhook_burst_suppression():
    """Phase 6: Duplicate delivery suppression (Burst simulation)"""
    db = MagicMock()
    locked_user = MagicMock(id="user-123", state="IDLE")
    
    # Mocking dependencies
    idempotency = MagicMock()
    # First call starts normally, subsequent calls see it "IN_PROGRESS" manually or via result=None
    idempotency.start.side_effect = [None, None, None] 
    idempotency.fetch_cached_response.return_value = None
    
    state_machine = MagicMock()
    state_machine.transition.return_value = MagicMock(allowed=True, next_state="LOAD_FLOW")
    
    extraction = ExtractionResult(intent=Intent.CREATE_LOAD, data={}, confidence=1.0, source="TEST", trace_id="t1")
    
    # We want to verify that if _phase2_atomic_dispatch is called multiple times,
    # and idempotency.start returns None (meaning it was already started by another worker),
    # the response is None (suppressed).
    
    with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
         patch("app.routers.webhook.get_or_create_session", return_value=None), \
         patch("app.routers.webhook.set_session_data", return_value=None), \
         patch("app.routers.webhook.update_session", return_value=None):
        
        # Simulation: 3 rapid calls
        results = await asyncio.gather(*[
            _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.burst",
                db=db,
                trace_id="trace-burst",
                intent=Intent.CREATE_LOAD,
                payload={},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine
            ) for _ in range(3)
        ])
        
    responses = [r[0] for r in results]
    # In a real burst, only one would succeed in idempotency.start() 
    # and return a real Response. The others return None.
    # Since we mocked idempotency.start to return None for all, all should be None.
    assert all(r is None for r in responses)

@pytest.mark.asyncio
async def test_success_only_replay_reuse():
    """Phase 7: SUCCESS-only replay reuse"""
    from app.services.idempotency_service import IdempotencyService
    
    # Case A: status = IN_PROGRESS
    record_inflight = MagicMock(status="IN_PROGRESS", request_payload={"extraction_data": {"lane": "A"}})
    service = IdempotencyService(db=None)
    service.find_record = MagicMock(return_value=record_inflight)
    
    assert service.fetch_cached_intent_data("wamid.123") is None
    
    # Case B: status = SUCCESS
    record_success = MagicMock(status="SUCCESS", intent=Intent.CREATE_LOAD, request_payload={"extraction_data": {"lane": "A"}})
    service.find_record = MagicMock(return_value=record_success)
    
    cached = service.fetch_cached_intent_data("wamid.123")
    assert cached is not None
    assert cached[0] == Intent.CREATE_LOAD
    assert cached[1]["lane"] == "A"
