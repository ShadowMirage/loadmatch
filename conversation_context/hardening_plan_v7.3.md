# LoadMatch Runtime Hardening Plan v7.3.1 (Final)

**Current Classification**: Tier-3.8 → Tier-4 upon Stage 1–4 completion.

---

## 🔄 13-Stage Rollout Sequence

### Stage 1 — Webhook Decomposition + Executor Scaling
- Decompose `receive_webhook` into `_phase1_resolve_intent`, `_phase2_atomic_dispatch`, `_phase3_send_response`.
- **Phase Boundary Contract**:
  - Phase 1: No DB writes except `ProcessedMessage` insert.
  - Phase 2: DB writes allowed (dispatcher execution).
  - Phase 3: No DB writes except delivery telemetry.
- Attach `ThreadPoolExecutor(max_workers=64)` to `app.state.executor`.
- **Files**: `webhook.py`, `main.py`.

### Stage 2 — Redis Watchdog + SQL Atomic Guard + Telemetry Migration
- **Corrected transaction boundary** (Redis check *before* dispatch):
```
BEGIN TX
  FOR UPDATE SKIP LOCKED
  Redis watchdog latency check (perf_counter)
  if elapsed > 50ms → set SQL-primary mode flag
  dispatch execution
  SQL fallback verify (delivered_at)
  delivered_at update
COMMIT
```
- Migrate DB: add `read_at`, `failed_at`, `execution_duration_ms`, `dispatch_started_at`.
- **Files**: `whatsapp_service.py`, `processed_message.py`.

### Stage 3 — Pydantic Extraction Migration
- `extraction_schemas.py` [NEW]: `LoadExtraction`, `TruckExtraction` Pydantic models.
- Replace dict parsing with `model_validate_json()` in `ai_extraction_service.py`.

### Stage 4 — Confidence Routing Activation
- Confidence map: `0.95` Pydantic → `0.70` Regex → `0.40` Heuristic → `0.10` UNKNOWN.
- Add `if confidence < 0.6: confirm_with_user()` in intent resolver.
- **Files**: `extraction_engine.py`, `intent_resolver.py`.

### Stage 5 — Extraction Determinism Validation
- `extraction_determinism_test.py`: same input → same intent, slots, confidence bucket, and dispatcher transition across workers and replays.

### Stage 6 — Extraction Arbitrator + Replay Hash
- `extraction_arbitrator.py` [NEW]: merges LLM/Regex/Heuristic → `ExtractionDecision`.
- Add `replay_execution_hash = hash(intent + slots + workflow_step)` to `ProcessedMessage`. On replay: if hash mismatch → abort → re-resolve intent.

### Stage 7 — ConversationStateGraph
- `conversation_state_graph.py` [NEW]: Redis hot state + Postgres snapshot.
- **Write optimization**: Snapshot only on `STATE_TRANSITION`, `DISPATCH_SUCCESS`, `CONFIRMATION_ACCEPTED`.
- Separate from `ProcessedMessage` and `WorkflowEvent`.

### Stage 8 — Slot Versioning Engine
- `slot_versioning_engine.py` [NEW].
- **Gates**: `slot_stability_index >= 0.92` AND `slot_reversal_rate < 0.05`.

### Stage 9 — Intent Drift Detector (Observe)
- `intent_drift_detector.py` [NEW]: `mode="observe"`, ≥ 500 sessions.
- Observe window must include weekday, weekend, peak, and low-volume traffic.

### Stage 10 — Telemetry Signal Adapter + Registry
- `telemetry_signal_adapter.py` [NEW]: converts raw events → routing signals.
- `telemetry_signal_registry.py` [NEW]: typed signal definitions to prevent naming drift.

### Stage 11 — Telemetry Feedback Engine
- `telemetry_feedback_engine.py` [NEW]: consumes normalized signals.

### Stage 12 — Adaptive Router (Shadow Mode)
- **Activation gates**: confidence routing + slot versioning + state graph + telemetry complete + `confidence_alignment >= 0.85` + `replay_reason_classifier` deployed.

### Stage 13 — Adaptive Router (Active) + Drift Active
- Switch router and drift detector to active operational modes.

---

## 🛡️ Contract Guards

| Guard | Scope |
| :--- | :--- |
| `dispatcher_contract_guard.py` | Cannot import: `whatsapp_service`, `redis_client`, `telemetry_writer`, `conversation_state_graph`, `adaptive_router`, `intent_drift_detector`. |
| `phase_boundary_contract_guard.py` | Enforces Phase 1/2/3 DB write isolation. |
| `state_consistency_guard.py` | `checkpoint_hash` mismatch → abort → reload → re-dispatch. |
| `replay_reason_classifier.py` | Enum: `DELIVERY_TIMEOUT`, `REDIS_LOCK_CONTENTION`, `DISPATCH_EXCEPTION`, `LOW_CONFIDENCE_EXTRACTION`, `WHATSAPP_429`, `UNKNOWN`. |

---

## 🧪 Verification Metrics

| Metric | Threshold |
| :--- | :--- |
| Slot Stability Index | ≥ 0.92 |
| Slot Reversal Rate | < 0.05 |
| Confidence Alignment | ≥ 0.85 |
| Confidence Variance (per intent) | < 0.12 |
| Extraction Determinism | 100% |

---

## 🏁 Tier-4 Certification Checklist

- [ ] Webhook 3-phase decomposition complete
- [ ] Redis latency watchdog implemented
- [ ] SQL fallback atomic integration complete
- [ ] Pydantic extraction migration complete
- [ ] Confidence routing active
- [ ] ThreadPoolExecutor(64) attached
- [ ] Telemetry lifecycle columns migrated
- [ ] Extraction determinism validated
- [ ] Replay execution hash implemented
- [ ] State snapshot hashing active
- [ ] Replay reason classifier active
