import os
import json
import time
import requests
from typing import List, Dict

# Configuration
BASE_URL = "http://localhost:8000"
WEBHOOK_URL = f"{BASE_URL}/webhook"
DEBUG_URL = f"{BASE_URL}/debug/dashboard"

TEST_CASES = [
    {
        "name": "HAPPY FLOW - 10 TON JAIPUR TO DELHI",
        "input": "10 ton Jaipur to Delhi tomorrow",
        "expect": ["confirm_load_request", "jaipur", "delhi", "10000"]
    },
    {
        "name": "MISSING DATA - SEND TRUCK JAIPUR",
        "input": "Send truck Jaipur",
        "expect": ["ask_missing_field", "field"]
    },
    {
        "name": "MIXED INPUT - COTTON 5 TON MONDAY",
        "input": "Jaipur Delhi 5 ton next monday cotton",
        "expect": ["confirm_load_request", "cotton", "5000"]
    },
    {
        "name": "AMBIGUOUS INPUT",
        "input": "maybe send something",
        "expect": ["fallback", "menu"]
    },
    {
        "name": "BUTTON TEST - TRACK BOOKING",
        "input": "track booking",
        "expect": ["track", "booking", "id"]
    }
]

class LoadMatchTester:
    def __init__(self):
        self.phone = "919999999999"
        self.results = []

    def mock_whatsapp_msg(self, text: str):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "123",
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": self.phone,
                            "id": f"msg_{int(time.time())}",
                            "timestamp": str(int(time.time())),
                            "text": {"body": text},
                            "type": "text"
                        }],
                        "contacts": [{"wa_id": self.phone, "profile": {"name": "Test User"}}]
                    },
                    "field": "messages"
                }]
            }]
        }
        return payload

    def run_tests(self):
        print("\n🚀 Starting LoadMatch Stability Test Harness\n" + "="*50)
        
        for case in TEST_CASES:
            print(f"Testing: {case['name']}...")
            try:
                # 1. Send Webhook
                resp = requests.post(WEBHOOK_URL, json=self.mock_whatsapp_msg(case['input']))
                
                # 2. Check Dashboard for AI result
                time.sleep(1) # Wait for processing
                db_resp = requests.get(DEBUG_URL)
                dashboard = db_resp.json()
                
                # Simple validation logic
                logs = dashboard.get("recent_logs", [])
                last_log = logs[0] if logs else {}
                
                passed = True
                reason = "All assertions passed"
                
                # Check expectation keywords in last log payload or AI output
                payload_str = json.dumps(last_log.get("payload", {})).lower()
                for keyword in case['expect']:
                    if keyword.lower() not in payload_str:
                        # Fallback check in response builder if reply was used
                        passed = False
                        reason = f"Keyword '{keyword}' not found in AI action/reply"
                        break
                
                self.results.append({
                    "case": case['name'],
                    "status": "PASS" if passed else "FAIL",
                    "reason": reason
                })
                print(f"  {'✅' if passed else '❌'} {case['name']}")
                
            except Exception as e:
                self.results.append({"case": case['name'], "status": "ERROR", "reason": str(e)})
                print(f"  💥 Error: {e}")

    def report(self):
        print("\n" + "="*50 + "\nFINAL STABILITY REPORT\n" + "="*50)
        total = len(self.results)
        passed = len([r for r in self.results if r['status'] == "PASS"])
        
        for r in self.results:
            print(f"[{r['status']}] {r['case']} - {r['reason']}")
            
        score = (passed/total)*100 if total > 0 else 0
        print(f"\nSystem Health Score: {score:.1f}% ({passed}/{total} passed)")
        print("="*50 + "\n")

if __name__ == "__main__":
    # Ensure server is running or this will fail
    tester = LoadMatchTester()
    tester.run_tests()
    tester.report()
