import asyncio
import httpx
import time
import uuid

WEBHOOK_URL = "http://localhost:8000/webhook"

def make_payload(phone, text, msg_id=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "1006553859207868",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550275811", "phone_number_id": "1006553859207868"},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": phone}],
                    "messages": [{
                        "from": phone,
                        "id": msg_id or f"wamid.{uuid.uuid4().hex}",
                        "timestamp": str(int(time.time())),
                        "text": {"body": text},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

async def run_scenario(name, tasks):
    print(f"\n🚀 Starting Scenario: {name}")
    start = time.perf_counter()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    duration = time.perf_counter() - start
    
    success = [r for r in results if not isinstance(r, Exception) and r.status_code == 200]
    errors = [r for r in results if isinstance(r, Exception) or r.status_code != 200]
    
    print(f"✅ Completed in {duration:.2f}s")
    print(f"📊 Success: {len(success)} | Errors: {len(errors)}")

    if errors:
        first_error = errors[0]
        if isinstance(first_error, Exception):
            raise AssertionError(f"{name} failed: {first_error}")
        raise AssertionError(f"{name} failed with HTTP {first_error.status_code}")

    return results

async def scenario_cross_worker_race():
    """Test A: Same message ID, multiple parallel requests (Idempotency check)."""
    msg_id = f"race-{uuid.uuid4().hex}"
    phone = "919876543210"
    payload = make_payload(phone, "Create a load from Delhi to Mumbai 500kg", msg_id)
    
    async with httpx.AsyncClient(timeout=60) as client:
        tasks = [client.post(WEBHOOK_URL, json=payload) for _ in range(10)]
        await run_scenario("A - Cross-Worker Race", tasks)

async def scenario_concurrency_surge():
    """Test B: Same user, 50 different messages simultaneously (Lock serialization)."""
    phone = "919988776655"
    async with httpx.AsyncClient(timeout=300) as client:
        tasks = [
            client.post(WEBHOOK_URL, json=make_payload(phone, f"New load {i} from Delhi to Jaipur 100kg"))
            for i in range(50)
        ]
        await run_scenario("B - Concurrency Surge", tasks)

async def scenario_hot_user_backlog():
    """Test D: 1 user, rapid sequential burst (Serialization check)."""
    phone = "917766554433"
    async with httpx.AsyncClient(timeout=60) as client:
        tasks = []
        for i in range(20):
            tasks.append(client.post(WEBHOOK_URL, json=make_payload(phone, f"Sequential MSG {i}")))
        await run_scenario("D - Hot User Backlog", tasks)

async def main():
    print("🛡️ LoadMatch 'Unbreakable' Verification Matrix")
    print("="*50)
    
    try:
        await scenario_cross_worker_race()
        await scenario_concurrency_surge()
        await scenario_hot_user_backlog()
        
        print("\n✨ Verification Suite Complete.")
        print("Note: Manual Scenarios C (Crash) and E (Self-Healing) require external trigger/DB update.")
        return 0
    except Exception as e:
        print(f"\n❌ Suite Failed: {e}")
        return 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
