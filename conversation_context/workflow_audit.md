# 🏗️ LoadMatch System Workflow & Stability Audit

This document maps the end-to-end workflows of the LoadMatch platform to their respective functions, identifying where stability is anchored and where logic risks remain.

---

## 🛤️ Workflow 1: Message Ingress & Idempotency (The Guard)
**Objective**: Ensure every WhatsApp message is received and processed exactly once.

| Phase | Principal Functions | Stability Anchor |
| :--- | :--- | :--- |
| **Verification** | `webhook.py:verify_webhook` | Standard HMAC/Token verification. |
| **Trace Init** | `webhook.py:receive_webhook` | `trace_context.set_trace_id` ensures logs are unified across workers. |
| **Idempotency** | `webhook.py:receive_webhook` | `ProcessedMessage` query with `REPLAY` check. Prevents duplicate work. |

**Critical Failure Point**: If the DB check fails (e.g., deadlock), the webhook may retry, but the first process may still be running.
**Stability Fix**: Row-level locking (`with_for_update`) on the `User` or `ProcessedMessage` during phase 1.

---

## 🧠 Workflow 2: Intent Resolution (The Intelligence)
**Objective**: Convert raw WhatsApp text/media into a structured logistics intent.

| Phase | Principal Functions | Stability Anchor |
| :--- | :--- | :--- |
| **Extraction** | `ExtractionEngine.extract` | **Circuit Breaker**: Redis-synced state prevents LLM-retry loops during outages. |
| **AI Parsing** | `ai_extraction_service:extract_with_context` | 3-stage fallback (Pydantic -> Regex -> Heuristic). |
| **Normalization** | `extraction_engine:normalize_entities` | Multi-key mapping ensures `from` always becomes `from_city`. |

**Critical Failure Point**: LLM non-determinism. Small changes in user wording can lead to low-confidence extraction.
**Stability Fix**: Confidence-based routing (Phase 12.1) to trigger user confirmation for scores < 0.6.

---

## ⚡ Workflow 3: Atomic Dispatch (The Execution)
**Objective**: Update system state (Create Load/Listing/Match) based on resolved intent.

| Phase | Principal Functions | Stability Anchor |
| :--- | :--- | :--- |
| **Routing** | `webhook.py:_phase2_atomic_dispatch` (Planned) | Single point of synchronization. |
| **Execution** | `DispatcherService.execute` | "Dumb" executor follows strict contracts. No side-effects outside DB. |
| **Tie-Breaking** | `matching_service:rank_matches` | Deterministic sorting using `match_id` ensures consistent user experience. |

**Critical Failure Point**: Atomic failure. If the matching logic crashes, the `ProcessedMessage` must be marked as `FAILED` so the user knows.
**Stability Fix**: Unified `try/except` around the dispatch phase that logs a `WorkflowEvent`.

---

## 🏁 Workflow 4: Resilient Egress (The Last Mile)
**Objective**: Deliver the response to WhatsApp with absolute delivery confirmation.

| Phase | Principal Functions | Stability Anchor |
| :--- | :--- | :--- |
| **Locking** | `whatsapp_service:_check_and_set_response_sent` | **Redis Latency Guard**: Fallback to SQL `delivered_at` if Redis lags > 50ms. |
| **Delivery** | `whatsapp_service:send_response` | Unified entry point ensures all egress (Text/List/Buttons) is guarded. |
| **Telemetry** | `whatsapp_service:send_text` | Captures `wamid` and updates `delivered_at` in Postgres. |

**Critical Failure Point**: WhatsApp API 429/500. Message is accepted by LoadMatch but fails to reach the user.
**Stability Fix**: `RecoveryDaemon` auto-replays `EXECUTING` tasks that haven't moved to `SUCCESS` within 60s.

---

## 🔄 Workflow 5: Autonomous Recovery (The Replay)
**Objective**: Automatically fix "stuck" or "failed" workflows without human intervention.

| Phase | Principal Functions | Stability Anchor |
| :--- | :--- | :--- |
| **Scanning** | `RecoveryDaemon.scan_and_replay` | Queries `status=EXECUTING` older than X seconds. |
| **Replay** | `recovery_service:send_with_backoff` | Jitter-based exponential backoff prevents "thundering herd" on the API. |

**Critical Failure Point**: Infinite loops. If a message is fundamentally "unsendable", it could drain resources.
**Stability Fix**: `retry_count` cap (3) and transition to `DLQ` if all attempts fail.

---

## 🛡️ Stability Checklist for "Bug-Free" Status
1.  **[ ] Zero-Leak Sessions**: Ensure `clear_session` is called after every successful `CONFIRM_LOAD/TRUCK`.
2.  **[ ] Contract-First**: Use Pydantic objects instead of `dict.get()` in the orchestrator.
3.  **[ ] Global Traceability**: Pass `trace_id` to every single background task.
4.  **[ ] Fail-Open Circuit**: If Redis is down, the system MUST fallback to the database/Local-Context, not crash.
5.  **[ ] SQL-Based Tie-Breaker**: All `ORDER BY` clauses in SQL must include a secondary unique ID.
