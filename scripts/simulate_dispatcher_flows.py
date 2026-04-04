import asyncio
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from app.services.dispatcher_service import DispatcherService
from app.contracts.enums import Intent

async def simulate_flow(message_text, expected_pacing_keyword):
    db = MagicMock()
    dispatcher = DispatcherService(db, user_id="user-123", phone="919999999999")
    
    # We use a real IntentResolver to get the metadata that Dispatcher needs
    from app.services.intent_resolver import IntentResolver
    from app.services.extraction_engine import ExtractionResult
    
    resolver = IntentResolver()
    extraction = ExtractionResult(intent=Intent.UNKNOWN, data={}, confidence=0.0, source="TEST", trace_id="t1")
    intent = resolver.resolve(extraction, None, "IDLE", message_text=message_text)
    
    payload = SimpleNamespace(data=extraction.data)
    
    with patch("app.services.dispatcher_service.get_session_data", return_value={}), \
         patch("app.services.dispatcher_service.set_session_data"):
        response = dispatcher.execute(intent, payload=payload)
    
    print(f"INPUT: '{message_text}'")
    print(f"BOT: '{response.text}'")
    
    if expected_pacing_keyword.lower() in response.text.lower():
        print(f"RESULT: PASS (Found '{expected_pacing_keyword}')")
        return True
    else:
        print(f"RESULT: FAIL (Expected keyword '{expected_pacing_keyword}' not found)")
        return False

async def main():
    print("--- Phase 12: Dispatcher Pacing Simulation ---")
    
    tests = [
        ("blr to delhi", "weight"), # HIGH confidence: skip route questions (City-pair / Alias-pair)
        ("delhi jaipur", "confirming the route"), # MEDIUM/LOW confidence: confirm once (Adjacent)
        ("random city maybe delhi?", "I didn't catch"), # UNKNOWN
    ]
    
    success_count = 0
    for text, keyword in tests:
        if await simulate_flow(text, keyword):
            success_count += 1
        print("-" * 20)
    
    if success_count == len(tests):
        print("Certification Phase 12: PASSED")
        exit(0)
    else:
        print(f"Certification Phase 12: FAILED ({success_count}/{len(tests)} passed)")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
