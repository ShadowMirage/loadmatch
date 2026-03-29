"""
LoadMatch Chaos Testing Engine
Injects real-world failure conditions and validates system resilience.
PASS only if ALL chaos scenarios are survived without crash.
"""
import asyncio
import os
import secrets
import json
import traceback
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator, String
from unittest.mock import AsyncMock, MagicMock, patch

# Patch JSONB for SQLite
import sqlalchemy.dialects.sqlite.base as sqlite_base
sqlite_base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

# ── Anthropic mock BEFORE app imports ─────────────────────────────────────
mock_ai_response = MagicMock()
mock_ai_response.content = [MagicMock(type="text", text="I need more info about your shipment.")]
mock_ai = MagicMock()
mock_ai.messages.create = AsyncMock(return_value=mock_ai_response)
_anthropic_patcher = patch('anthropic.AsyncAnthropic', return_value=mock_ai)
_anthropic_patcher.start()

from app.database import build_engine, create_schema
from app.models.user import User, UserRole
from app.models.user_activity import UserActivity
from app.models.conversation import Conversation
from app.models.user_session import UserSession
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.match import Match
from app.models.kyc import KycDocument
from app.models.route_subscription import RouteSubscription

from app.routers.webhook import receive_webhook
from fastapi import Request
from starlette.datastructures import Headers

# ── Standard WA response ───────────────────────────────────────────────────
def _ok_client():
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"messages": [{"id": "wamid.test"}]}
    r.raise_for_status = MagicMock()
    r.text = "{}"
    r.content = b""
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.post = AsyncMock(return_value=r)
    c.get  = AsyncMock(return_value=r)
    return c

def _error_client(status_code):
    import httpx
    r = MagicMock()
    r.status_code = status_code
    r.text = f"Error {status_code}"
    r.raise_for_status = MagicMock(
        side_effect=lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                f"{status_code} Error", request=MagicMock(), response=r
            )
        )
    )
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.post = AsyncMock(return_value=r)
    c.get  = AsyncMock(return_value=r)
    return c

# ── DB ─────────────────────────────────────────────────────────────────────
engine = build_engine("sqlite:///:memory:")
create_schema(bind=engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class MockRequest(Request):
    def __init__(self, data):
        super().__init__({"type": "http"})
        self._data = data

    async def json(self):
        return self._data

    @property
    def headers(self):
        return Headers({"content-type": "application/json"})


def _make_body(msg, phone="919999999999"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "1", "phone_number_id": "2"},
            "contacts": [{"profile": {"name": "Tester"}, "wa_id": phone}],
            "messages": [{**msg, "from": phone}]
        }, "field": "messages"}]}]
    }


async def run(db, text_body, phone="919999999999"):
    from app.services.whatsapp_service import reset_response_guard
    reset_response_guard()
    req = MockRequest(_make_body({
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "text",
        "text": {"body": text_body}
    }, phone))
    await receive_webhook(request=req, db=db)


async def run_image(db, phone="919999999999"):
    from app.services.whatsapp_service import reset_response_guard
    reset_response_guard()
    req = MockRequest(_make_body({
        "id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now().timestamp())),
        "type": "image",
        "image": {"mime_type": "image/jpeg", "sha256": "xyz", "id": "media_chaos"}
    }, phone))
    await receive_webhook(request=req, db=db)


# ══════════════════════════════════════════════════════════════════════════
# CHAOS HARNESS
# ══════════════════════════════════════════════════════════════════════════
results = []

async def chaos(label, coro):
    print(f"\n{'─'*60}")
    print(f"💥 CHAOS: {label}")
    print(f"{'─'*60}")
    crashed = False
    crash_msg = ""
    try:
        await coro
    except SystemExit:
        crashed = True
        crash_msg = "SystemExit raised"
    except Exception as e:
        # If the exception escapes the webhook boundary, that is a crash
        crashed = True
        crash_msg = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    status = "❌ FAIL — CRASHED" if crashed else "✅ PASS — survived"
    print(f"  {status}")
    if crashed:
        print(f"  REASON: {crash_msg}")

    results.append({
        "label": label,
        "passed": not crashed,
        "crash": crash_msg
    })
    return not crashed


# ══════════════════════════════════════════════════════════════════════════
async def run_chaos():
    db = TestingSessionLocal()
    user = User(phone="919999999999", name="Chaos Tester", role=UserRole.shipper,
                wa_onboarded=True, language="en")
    db.add(user)
    db.commit()

    # ── C1: AI returns raw malformed JSON ─────────────────────
    bad_ai = MagicMock()
    bad_ai.content = [MagicMock(type="text", text='{"action": "confirm_truck_listing", "data": {broken_json!!!')]
    mock_ai.messages.create = AsyncMock(return_value=bad_ai)
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C1: AI returns malformed JSON",
                        run(db, "jaipur to delhi 5 ton tomorrow"))
    await asyncio.sleep(1)

    # ── C1b: AI returns empty content array ────────────────────
    empty_ai = MagicMock()
    empty_ai.content = []  # empty => no text block
    mock_ai.messages.create = AsyncMock(return_value=empty_ai)
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C1b: AI returns empty content array",
                        run(db, "empty response test"))
    await asyncio.sleep(1)

    # ── C1c: AI raises exception ────────────────────────────────
    mock_ai.messages.create = AsyncMock(side_effect=RuntimeError("Anthropic API down"))
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C1c: AI raises RuntimeError",
                        run(db, "route query"))
    mock_ai.messages.create = AsyncMock(return_value=mock_ai_response)  # restore
    await asyncio.sleep(1)

    # ── C2: WhatsApp API 400 ───────────────────────────────────
    with patch('httpx.AsyncClient', return_value=_error_client(400)):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C2: WhatsApp API returns 400",
                        run(db, "hello"))
    await asyncio.sleep(1)

    # ── C3: WhatsApp API 500 ───────────────────────────────────
    with patch('httpx.AsyncClient', return_value=_error_client(500)):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C3: WhatsApp API returns 500",
                        run(db, "hello"))
    await asyncio.sleep(1)

    # ── C4: S3 upload fails ────────────────────────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.storage_service.upload_whatsapp_media',
                   AsyncMock(side_effect=ConnectionError("S3 bucket unreachable"))):
            with patch('app.services.kyc_service.handle_kyc_image',
                       AsyncMock(side_effect=ConnectionError("S3 failed"))):
                await chaos("C4: S3 upload fails (KYC image)",
                            run_image(db))
    await asyncio.sleep(1)

    # ── C5: DB commit fails ────────────────────────────────────
    from unittest.mock import patch as _patch
    orig_commit = db.commit

    call_count = {"n": 0}
    def flaky_commit():
        call_count["n"] += 1
        if call_count["n"] <= 2:  # first 2 commits fail
            raise Exception("DB commit failed: disk full")
        return orig_commit()

    db.commit = flaky_commit
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C5: DB commit fails mid-flow",
                        run(db, "hello"))
    db.commit = orig_commit  # restore
    await asyncio.sleep(1)

    # ── C6: Messy real-world Hindi input ──────────────────────
    mock_ai.messages.create = AsyncMock(return_value=mock_ai_response)
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C6: Messy input 'jaipur se delhi kal 5 ton'",
                        run(db, "jaipur se delhi kal 5 ton"))
    await asyncio.sleep(1)

    # ── C7: Junk truck query with invalid chars ────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C7: Junk input 'truck RJ14??'",
                        run(db, "truck RJ14??"))
    await asyncio.sleep(1)

    # ── C8: Completely nonsense message ───────────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C8: Nonsense 'send anything bro'",
                        run(db, "send anything bro"))
    await asyncio.sleep(1)

    # ── C9: SQL injection attempt ──────────────────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C9: SQL injection attempt",
                        run(db, "'; DROP TABLE users; --"))
    await asyncio.sleep(1)

    # ── C10: Extremely long message (10KB) ─────────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C10: Extremely long message (10,000 chars)",
                        run(db, "A" * 10000))
    await asyncio.sleep(1)

    # ── C11: Empty string ──────────────────────────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C11: Empty string input",
                        run(db, ""))
    await asyncio.sleep(1)

    # ── C12: Unicode / emoji bomb ──────────────────────────────
    with patch('httpx.AsyncClient', return_value=_ok_client()):
        with patch('app.services.kyc_service.handle_kyc_image', AsyncMock(return_value=True)):
            await chaos("C12: Unicode / emoji bomb input",
                        run(db, "🔥💣🚀" * 300 + " jaipur to delhi"))
    await asyncio.sleep(1)

    db.close()


def print_report(results):
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    print("\n\n" + "═"*65)
    print("💥 CHAOS TEST REPORT — LoadMatch")
    print("═"*65)
    print(f"\n{'ID':<5} {'Scenario':<48} {'Result'}")
    print("─"*65)
    for i, r in enumerate(results, 1):
        status = "✅ PASS" if r["passed"] else "❌ FAIL"
        name = r["label"][:47]
        print(f"  {i:<4} {name:<48} {status}")
        if r["crash"]:
            print(f"       ↳ Crash: {r['crash']}")

    print(f"\n{'─'*65}")
    print(f"  Passed: {passed}/{total}")
    print(f"  Failed: {failed}/{total}")
    print()

    if failed == 0:
        verdict = "✅ CHAOS SURVIVED — System is resilient"
        badge = "🟢"
    elif failed <= 2:
        verdict = "⚠️  MOSTLY SURVIVED — Minor hardening needed"
        badge = "🟡"
    else:
        verdict = "🔴 CHAOS FAILURES — Critical fixes required"
        badge = "🔴"

    print(f"  {badge} VERDICT: {verdict}")
    print("═"*65 + "\n")

    return {"passed": passed, "total": total, "failed": failed, "verdict": verdict}


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    os.environ['DATABASE_URL'] = "sqlite:///:memory:"

    asyncio.run(run_chaos())
    summary = print_report(results)

    with open("chaos_report.json", "w") as f:
        json.dump({"summary": summary, "tests": results}, f, indent=2)
    print("✅ Saved: chaos_report.json")
