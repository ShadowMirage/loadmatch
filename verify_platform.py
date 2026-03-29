import asyncio
import logging
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

# Keep platform verification focused on orchestration behavior rather than live infra.
mock_ai_response = MagicMock()
mock_ai_response.content = [MagicMock(type="text", text='{"action":"general_chat","data":{}}')]
mock_messages = MagicMock()
mock_messages.create = AsyncMock(return_value=mock_ai_response)
mock_anthropic = MagicMock()
mock_anthropic.messages = mock_messages
patch("anthropic.AsyncAnthropic", return_value=mock_anthropic).start()

mock_wa_response = MagicMock()
mock_wa_response.status_code = 200
mock_wa_response.json.return_value = {"messages": [{"id": "wamid.test"}]}
mock_wa_response.raise_for_status = MagicMock()
mock_wa_response.text = "{}"
mock_wa_response.content = b""

mock_http_client = MagicMock()
mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
mock_http_client.__aexit__ = AsyncMock(return_value=False)
mock_http_client.post = AsyncMock(return_value=mock_wa_response)
mock_http_client.get = AsyncMock(return_value=mock_wa_response)
patch("httpx.AsyncClient", return_value=mock_http_client).start()

from app.database import SessionLocal, create_schema
from app.models.user import User
from app.routers.webhook import receive_webhook

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_concurrent_idempotency():
    """Simulate 2 identical messages arriving at once."""
    db = SessionLocal()
    phone = "919876543210"
    wa_id = f"msg_{uuid4()}"
    
    # Mocking request body
    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": phone,
                        "id": wa_id,
                        "type": "text",
                        "text": {"body": "Jaipur to Delhi 10 ton tomorrow"}
                    }]
                }
            }]
        }]
    }

    async def run_req(idx):
        logger.info(f"Starting request {idx}")
        mock_req = MagicMock()
        
        async def mock_json():
            return body
            
        mock_req.json = mock_json
        return await receive_webhook(mock_req, db)

    # Run both simultaneously
    results = await asyncio.gather(run_req(1), run_req(2), return_exceptions=True)
    for i, res in enumerate(results):
        logger.info(f"Request {i+1} Result: {res}")
    db.close()

async def test_atomic_rollback_on_crash():
    """Simulate a crash during Phase 2 execution."""
    db = SessionLocal()
    phone = "919876543211"
    wa_id = f"msg_{uuid4()}"
    
    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": phone,
                        "id": wa_id,
                        "type": "text",
                        "text": {"body": "Jaipur to Delhi 5 ton tomorrow"}
                    }]
                }
            }]
        }]
    }

    # Manually crash the dispatcher
    with patch("app.services.dispatcher_service.DispatcherService.execute") as mock_exec:
        mock_exec.side_effect = Exception("🔥 KABOOM - CRASH INCIDENT")
        
        mock_req = MagicMock()
        async def mock_json():
            return body
        mock_req.json = mock_json
        
        try:
            await receive_webhook(mock_req, db)
        except Exception as e:
            logger.info(f"Caught expected crash: {e}")

    # VERIFY using a fresh session so rollback checks are not polluted by identity-map state.
    db.close()
    verify_db = SessionLocal()
    user = verify_db.query(User).filter(User.phone == phone).first()
    logger.info(f"User state after crash: {user.state if user else 'N/A'}")

    from app.models.processed_message import ProcessedMessage
    idem = verify_db.query(ProcessedMessage).filter(ProcessedMessage.idempotency_key.contains(wa_id)).first()
    logger.info(
        "Crash ledger record: exists=%s status=%s",
        idem is not None,
        getattr(idem, "status", None),
    )
    verify_db.close()

if __name__ == "__main__":
    create_schema()
    asyncio.run(test_concurrent_idempotency())
    asyncio.run(test_atomic_rollback_on_crash())
