import asyncio
import os
import secrets
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator, String
from unittest.mock import AsyncMock, MagicMock, patch

# Patch JSONB for SQLite compatibility
import sqlalchemy.dialects.sqlite.base as sqlite_base
sqlite_base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

# ── Mocks BEFORE any app imports ──────────────────────────────────────────
# Mock Anthropic client so it doesn't call the real API
mock_ai_response = MagicMock()
mock_ai_response.content = [MagicMock(text="I need more info about your shipment.")]
mock_messages = MagicMock()
mock_messages.create = AsyncMock(return_value=mock_ai_response)
mock_anthropic = MagicMock()
mock_anthropic.messages = mock_messages
patch('anthropic.AsyncAnthropic', return_value=mock_anthropic).start()

# THEN import app modules
from app.database import build_engine, create_schema
from app.models.user import User
from app.models.user_activity import UserActivity
from app.models.conversation import Conversation
from app.models.user_session import UserSession
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.match import Match
from app.models.kyc import KycDocument
from app.models.route_subscription import RouteSubscription

# Now mock WhatsApp HTTP AFTER app modules are imported
mock_wa_response = MagicMock()
mock_wa_response.status_code = 200
mock_wa_response.json.return_value = {"messages": [{"id": "wamid.test"}]}
mock_wa_response.raise_for_status = MagicMock()
mock_wa_response.text = "{}"

mock_http_client = MagicMock()
mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
mock_http_client.__aexit__ = AsyncMock(return_value=False)
mock_http_client.post = AsyncMock(return_value=mock_wa_response)
mock_http_client.get  = AsyncMock(return_value=mock_wa_response)

patch('httpx.AsyncClient', return_value=mock_http_client).start()
patch('app.services.storage_service.upload_whatsapp_media',
      AsyncMock(return_value="https://test.s3/image.jpg")).start()

# Patch KYC to not make real S3 calls
patch('app.services.kyc_service.handle_kyc_image',
      AsyncMock(return_value=True)).start()

from app.routers.webhook import receive_webhook
from fastapi import Request
from starlette.datastructures import Headers

# ── SQLite in-memory DB ────────────────────────────────────────────────────
engine = build_engine("sqlite:///:memory:")
create_schema(bind=engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class MockRequest(Request):
    def __init__(self, json_data):
        super().__init__({"type": "http"})
        self._json_data = json_data

    async def json(self):
        return self._json_data

    @property
    def headers(self):
        return Headers({"content-type": "application/json"})


def _make_body(message_payload):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "123456789",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "15551234567",
                        "phone_number_id": "15551234568"
                    },
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": "919999999999"}],
                    "messages": [message_payload]
                },
                "field": "messages"
            }]
        }]
    }


async def simulate_webhook(db_session, label, message_payload):
    print(f"\n{'='*60}")
    print(f"🧪 TEST: {label}")
    print(f"{'='*60}")
    # Reset double-response guard per contextvars between tests
    from app.services.whatsapp_service import reset_response_guard
    reset_response_guard()

    request = MockRequest(_make_body(message_payload))
    await receive_webhook(request=request, db=db_session)


async def run_tests():
    db = TestingSessionLocal()
    from app.models.user import UserRole
    user = User(phone="919999999999", name="Test User", role=UserRole.shipper,
                wa_onboarded=True, language="en")
    db.add(user)
    db.commit()

    # ── TEST 1: greeting ───────────────────────────────────────────────
    await simulate_webhook(db, "hello", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "text",
        "text": {"body": "hello"}
    })
    await asyncio.sleep(1)

    # ── TEST 2: full load query ────────────────────────────────────────
    await simulate_webhook(db, "7 tons cotton from jaipur to gwalior", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "text",
        "text": {"body": "7 tons cotton from jaipur to gwalior"}
    })
    await asyncio.sleep(1)

    # ── TEST 3: Hindi real-world input (se→to, kal→tomorrow) ──────────
    await simulate_webhook(db, "jaipur se gwalior 5 ton kal", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "text",
        "text": {"body": "jaipur se gwalior 5 ton kal"}
    })
    await asyncio.sleep(1)

    # ── TEST 4: button click – Delivery Status ────────────────────────
    await simulate_webhook(db, "click: Delivery Status", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "Delivery Status", "title": "Delivery Status"}
        }
    })
    await asyncio.sleep(1)

    # ── TEST 5: partial data (weight only) ────────────────────────────
    await simulate_webhook(db, "5 ton (partial weight only)", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "text",
        "text": {"body": "5 ton"}
    })
    await asyncio.sleep(1)

    # ── TEST 6: truck number plate ────────────────────────────────────
    await simulate_webhook(db, "RJ14AB1234 (truck plate)", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "text",
        "text": {"body": "RJ14AB1234"}
    })
    await asyncio.sleep(1)

    # ── TEST 7: image upload (KYC flow) ───────────────────────────────
    await simulate_webhook(db, "image upload (KYC)", {
        "from": "919999999999",
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "image",
        "image": {
            "mime_type": "image/jpeg",
            "sha256": "fake_sha",
            "id": "fake_media_id_12345"
        }
    })

    print("\n" + "="*60)
    print("✅ All 7 tests complete.")
    print("="*60)
    db.close()


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    os.environ['DATABASE_URL'] = "sqlite:///:memory:"
    asyncio.run(run_tests())
