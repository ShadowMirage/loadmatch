import logging
import dataclasses
import time
from uuid import uuid4
from datetime import date, datetime, timezone
from typing import Optional, Tuple, Any

from fastapi import APIRouter, Request, Response as FastAPIResponse, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.trace_context import set_trace_id
from app.database import get_db
from app.models.user import User
from app.runtime.whatsapp_adapter import verify_token as get_verify_token
from app.contracts.meta_intents import INTERRUPT_INTENTS

from app.services.session_manager import (
    clear_session,
    get_or_create_session,
    get_session_data,
    peek_session,
    set_session_data,
    update_session,
)
from app.contracts.extraction import ExtractionResult
from app.services.extraction_engine import ExtractionEngine
from app.services.intent_resolver import IntentResolver
from app.services.payload_factory import PayloadFactory
from app.services.state_machine_service import StateMachineService
from app.services.idempotency_service import IdempotencyService
from app.services.dispatcher_service import DispatcherService
from app.services.recovery_service import RecoveryService
from app.services.event_bus import EventBus
from app.services import kyc_service
from app.services.rate_limiter import check as rate_limit_check
from app.services.whatsapp_service import send_text, safe_fallback
from app.models.processed_message import ProcessedMessage, WorkflowEvent

# ✅ SINGLE SOURCE OF TRUTH
from app.contracts.responses import Response as ContractResponse
from app.contracts.enums import Intent

logger = logging.getLogger("loadmatch.webhook")
router = APIRouter(prefix="/webhook", tags=["Webhook"])


class AtomicDispatchError(RuntimeError):
    def __init__(self, message: str, *, idem_key: Optional[str] = None):
        super().__init__(message)
        self.idem_key = idem_key


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _extract_messages(body: dict) -> list[dict]:
    messages: list[dict] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages.extend(value.get("messages") or [])
    return messages


def _get_or_create_user(db: Session, phone: str) -> User:
    user = db.query(User).filter(User.phone == phone).one_or_none()
    if user:
        return user

    try:
        with db.begin_nested():
            user = User(phone=phone)
            db.add(user)
            db.flush()
            db.refresh(user)
    except IntegrityError:
        user = db.query(User).filter(User.phone == phone).one()

    return user


def _lock_user_for_dispatch(db: Session, phone: str) -> User:
    query = db.query(User).filter(User.phone == phone)
    if getattr(db.bind, "dialect", None) is not None and db.bind.dialect.name != "sqlite":
        query = query.with_for_update()
    return query.one()


def _mark_failed_after_rollback(db: Session, idem_key: Optional[str]) -> None:
    if not idem_key:
        return

    recovery_db = Session(bind=db.get_bind())
    try:
        IdempotencyService(recovery_db).mark_failed(idem_key)
        recovery_db.commit()
    except Exception:
        recovery_db.rollback()
        logger.exception("Failed to persist FAILED status for idempotency key %s", idem_key)
    finally:
        recovery_db.close()


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------

@router.get("")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    verify_token = get_verify_token()
    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        return FastAPIResponse(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------------------------
# Phase 1: Resolve Intent (No DB writes except ProcessedMessage insert)
# ---------------------------------------------------------------------------

async def _phase1_resolve_intent(
    msg: dict,
    phone: str,
    wa_id: Optional[str],
    user: User,
    db: Session,
    extraction_engine: ExtractionEngine,
    intent_resolver: IntentResolver,
    payload_factory: PayloadFactory,
    idempotency: IdempotencyService,
) -> Tuple[Intent, Any, ExtractionResult, Optional[str]]:
    """
    Phase 1: Extraction, normalization, confidence assessment.
    DB writes: ProcessedMessage insert ONLY (via idempotency check).
    """
    msg_type = msg.get("type")
    raw_text = msg.get("text", {}).get("body", "").strip() if msg_type == "text" else ""

    interactive_payload = None
    if msg_type == "interactive":
        interactive_payload = msg.get("interactive", {}).get("button_reply") or \
                              msg.get("interactive", {}).get("list_reply")

    # Idempotency early-exit: If message was already successfully handled, skip extraction
    # but return enough context to allow Phase 2/3 to replay the response.
    cached_intent_data = idempotency.fetch_cached_intent_data(wa_id) if wa_id else None
    if cached_intent_data:
        intent_val, data_val = cached_intent_data
        logger.info(f"Replay detected for {wa_id}. Bypassing extraction.")
        return intent_val, None, ExtractionResult(intent=intent_val, data=data_val, confidence=1.0, source="CACHE", trace_id=trace_id), None

    session = peek_session(db, phone)
    session_data = get_session_data(db, phone, user.id, create=False)
    session_data = session_data if isinstance(session_data, dict) else {}
    current_workflow = None

    stored_workflow = getattr(session, "current_workflow", None)
    if stored_workflow is None:
        stored_workflow = getattr(user, "state", None)

    reconstructed_workflow = StateMachineService.reconstruct_workflow(stored_workflow, session_data)
    if reconstructed_workflow:
        current_workflow = reconstructed_workflow
    elif stored_workflow:
        session_data.clear()

    extraction = await extraction_engine.extract(raw_text, user, session_data)
    logger.info(f"[EXTRACTION] intent={extraction.intent} fresh_data={extraction.data}")
    intent = intent_resolver.resolve(extraction, interactive_payload, current_workflow, message_text=raw_text)

    # Support partial updates (corrections): merge prior session_data with fresh extraction
    # Fresh extraction keys take priority.
    effective_session_data = session_data if isinstance(session_data, dict) else {}
    interaction_data = {}
    if interactive_payload:
        interaction_data = {
            "interactive_action_id": interactive_payload.get("id"),
            "interactive_action_title": interactive_payload.get("title"),
        }
    if extraction.data:
        extraction.data = {**effective_session_data, **interaction_data, **extraction.data}
    else:
        extraction.data = {**effective_session_data, **interaction_data}
    
    logger.info(f"[MERGED_DATA] intent={intent} merged_data={extraction.data}")

    # Attempt payload construction. If it fails (missing fields), we don't crash.
    # Interrupt intents (GREETING, MENU, UNKNOWN) carry no domain payload — skip build.
    payload = None
    if intent not in INTERRUPT_INTENTS:
        try:
            payload = payload_factory.build(intent, extraction.data)
        except Exception as e:
            logger.info(f"Payload validation deferred for {intent.value}: {e}")
            # If we're already in a workflow, stay in it but ask for missing slots
            intent = Intent.UNKNOWN

    return intent, payload, extraction, current_workflow


# ---------------------------------------------------------------------------
# Phase 2: Atomic Dispatch (DB writes allowed — dispatcher execution)
# ---------------------------------------------------------------------------

async def _phase2_atomic_dispatch(
    phone: str,
    wa_id: Optional[str],
    db: Session,
    trace_id: str,
    intent: Intent,
    payload: Any,
    extraction: ExtractionResult,
    idempotency: IdempotencyService,
    state_machine: StateMachineService,
) -> Tuple[Optional[ContractResponse], Optional[str], Optional[str]]:
    """
    Phase 2: Row-locking, state machine transition, dispatcher execution.
    DB writes: Dispatcher mutations, state transitions.
    Returns: (response, idem_key, pm_record_id)
    """
    response = None
    idem_key = None
    msg_id = None

    tx_start = time.perf_counter()
    lock_wait_time = 0.0
    dispatcher_time = 0.0

    # 1. Row-level lock acquisition
    lock_start = time.perf_counter()
    locked_user = _lock_user_for_dispatch(db, phone)
    lock_wait_time = time.perf_counter() - lock_start

    # Capture current state BEFORE transition for dispatcher context
    current_db_state = getattr(locked_user, "state", "IDLE")

    transition = state_machine.transition(
        current_db_state,
        intent,
        locked_user.updated_at or datetime.now(timezone.utc)
    )

    if intent not in INTERRUPT_INTENTS and not transition.allowed:
        logger.warning(f"Transition denied: {transition.error_message}")
        response = ContractResponse(text=transition.error_message or "⚠️ Action not allowed.")
        # Even on denial, we might want to save data if it was a correction attempt
    else:
        # Construct idempotency key (stable across replays)
        idem_key = f"{locked_user.id}:{intent.value}:{wa_id}:{transition.next_state}"
        ranking_context_timestamp = datetime.now(timezone.utc).isoformat()

        if isinstance(extraction.data, dict):
            extraction.data["ranking_context_timestamp"] = ranking_context_timestamp
        if isinstance(payload, dict):
            payload.setdefault("ranking_context_timestamp", ranking_context_timestamp)
        elif hasattr(payload, "data") and isinstance(getattr(payload, "data"), dict):
            payload.data.setdefault("ranking_context_timestamp", ranking_context_timestamp)
        if hasattr(payload, "extraction_data") and isinstance(getattr(payload, "extraction_data"), dict):
            payload.extraction_data["ranking_context_timestamp"] = ranking_context_timestamp

        # Persist both the dispatcher payload and the full normalized extraction
        # context so replay can rebuild typed payloads without losing routing metadata.
        try:
            payload_dict = PayloadFactory.serialize(payload)
        except Exception:
            payload_dict = {"action": str(intent.value), "data": str(payload)}
        payload_dict = _json_safe(payload_dict)
        extraction_data = _json_safe(extraction.data or {})

        request_payload = {
            "intent": intent.value,
            "payload": payload_dict,
            "extraction_data": extraction_data,
            "phone": phone,
            "wa_id": wa_id,
            "current_workflow": current_db_state,
            "ranking_context_timestamp": ranking_context_timestamp,
        }

        # 2. Check/Start Idempotency Record
        pm_record: ProcessedMessage = idempotency.start(
            idem_key,
            trace_id,
            request_payload,
            wamid=wa_id,
            user_id=locked_user.id,
            intent=intent.value,
            confidence=extraction.confidence,
            workflow_step=transition.next_state,
            dispatcher_action=intent.value,
            delivery_state="PENDING",
        )
        
        if pm_record:
            # Apply state transition
            locked_user.state = transition.next_state

            # 3. Dispatcher Execution
            dispatch_start = time.perf_counter()
            dispatcher = DispatcherService(db, locked_user.id, phone=phone)

            try:
                # Pass pre-transition state to dispatcher
                response = dispatcher.execute(intent, payload, current_workflow=current_db_state)
                dispatcher_time = time.perf_counter() - dispatch_start
                idempotency.complete(idem_key, dataclasses.asdict(response))
            except Exception as e:
                logger.error(f"Execution failed for {idem_key}: {e}", exc_info=True)
                raise AtomicDispatchError(str(e), idem_key=idem_key) from e
            
            msg_id = pm_record.id
        else:
            # Message already processing or completed; fetch existing response
            cached_resp = idempotency.fetch_cached_response(wa_id) if wa_id else None
            if cached_resp:
                response = ContractResponse(**cached_resp)
            else:
                logger.info(f"Duplicate in-flight message detected for {wa_id}; suppressing secondary response.")
                response = None

    next_session_workflow = getattr(locked_user, "state", "IDLE")
    # Preserve slot state only while a workflow remains active. IDLE transitions
    # must clear the session buffer, otherwise cancel/confirm paths repopulate
    # stale payload data and the next workflow resumes with old slots.
    if not StateMachineService.workflow_is_active(next_session_workflow):
        persisted_session_data = get_session_data(db, phone, locked_user.id, create=False)
        if persisted_session_data:
            leaked_fields = {
                field: persisted_session_data.get(field)
                for field in StateMachineService.PROVENANCE_FIELDS
                if persisted_session_data.get(field)
            }
            if leaked_fields:
                logger.warning(
                    "Inactive workflow retained provenance metadata in session_data",
                    extra={
                        "phone": phone,
                        "workflow": next_session_workflow,
                        "leaked_fields": sorted(leaked_fields.keys()),
                    },
                )
            StateMachineService.cleanup_terminal_state(persisted_session_data)
        clear_session(db, phone)
    else:
        # 4. Universal Session Persistence (Correction Safety)
        # We save session data even if dispatch was skipped/denied to preserve conversational context.
        get_or_create_session(db, phone, locked_user.id)
        set_session_data(db, phone, locked_user.id, extraction.data)
        update_session(
            db,
            phone,
            {"current_workflow": next_session_workflow},
            commit=False,
        )

    # 5. Atomic Commit (Single Source of Truth)
    db.commit()

    # Metrics
    transaction_duration = time.perf_counter() - tx_start
    logger.info(
        f"[PHASE2_METRICS] user={locked_user.id} lock={lock_wait_time:.4f} "
        f"dispatch={dispatcher_time:.4f} tx={transaction_duration:.4f}"
    )

    return response, idem_key, msg_id


# ---------------------------------------------------------------------------
# Phase 3: Send Response (No DB writes except delivery telemetry)
# ---------------------------------------------------------------------------

async def _phase3_send_response(
    phone: str,
    wa_id: Optional[str],
    response: Optional[ContractResponse],
    idem_key: Optional[str],
    msg_id: Optional[str],
    db: Session,
    recovery: RecoveryService,
    idempotency: IdempotencyService,
) -> None:
    """
    Phase 3: Egress delivery and telemetry update.
    DB writes: WorkflowEvent (delivery telemetry) ONLY.
    """
    if not response:
        return

    try:
        success = await recovery.send_with_backoff(phone, response, retries=1, wa_id=wa_id)
        if success:
            if idem_key:
                # Bug #4 Fix: Reuse idempotency service from orchestrator param
                idempotency.mark_delivered(idem_key)

            if msg_id:
                db.add(WorkflowEvent(
                    processed_message_id=msg_id,
                    event_type="DELIVERY_SUCCESS",
                    trace_id=wa_id,
                    payload={"source": "webhook_immediate"}
                ))
                db.commit()
    except Exception as e:
        logger.error(f"Egress failed for {idem_key}: {e}")
        # RecoveryDaemon handles retries for SUCCESS jobs with delivered_at=NULL


# ---------------------------------------------------------------------------
# Incoming message handler (Orchestrator)
# ---------------------------------------------------------------------------

async def _process_message(msg: dict, db: Session) -> None:
    trace_id = str(uuid4())
    set_trace_id(trace_id)

    event_bus = EventBus(trace_id)
    extraction_engine = ExtractionEngine(trace_id)
    intent_resolver = IntentResolver()
    payload_factory = PayloadFactory()
    state_machine = StateMachineService()
    idempotency = IdempotencyService(db)
    recovery = RecoveryService(db)

    phone = msg.get("from")
    wa_id = msg.get("id")
    msg_type = msg.get("type")

    try:
        if not phone:
            return

        if wa_id and idempotency.exists(wa_id):
            logger.info(f"Duplicate message detected before processing (wa_id: {wa_id}). Skipping.")
            return

        user = _get_or_create_user(db, phone)

        if msg_type == "text":
            raw_text = msg.get("text", {}).get("body", "").strip()
            if rate_limit_check(db, str(user.id), raw_text):
                db.commit()
                await send_text(
                    phone,
                    "Too many repeated or invalid messages. Please slow down and send one clear request.",
                    wa_id=wa_id,
                )
                return

        if msg_type in {"image", "document"}:
            media_id = msg.get(msg_type, {}).get("id")
            if not media_id:
                await send_text(phone, "⚠️ I could not read that document. Please try uploading it again.", wa_id=wa_id)
                return

            media_key = f"{user.id}:UPLOAD_KYC:{wa_id}:IDLE"
            pm_record = idempotency.start(
                media_key,
                trace_id,
                {
                    "intent": Intent.UPLOAD_KYC.value,
                    "payload": {"media_id": media_id, "message_type": msg_type},
                    "phone": phone,
                    "wa_id": wa_id,
                    "current_workflow": getattr(user, "state", "IDLE"),
                },
                wamid=wa_id,
                user_id=user.id,
                intent=Intent.UPLOAD_KYC.value,
                confidence=100,
                workflow_step="UPLOAD_KYC",
                dispatcher_action="UPLOAD_KYC",
                delivery_state="PENDING",
            )
            if not pm_record:
                return

            try:
                await kyc_service.handle_kyc_image(user.id, phone, media_id, db)
                idempotency.complete(media_key, {"status": "received"})
                idempotency.mark_delivered(media_key)
                db.commit()
            except Exception:
                db.rollback()
                idempotency.mark_failed(media_key)
                db.commit()
                raise
            return

        # === PHASE 1: Resolve Intent ===
        intent, payload, extraction, current_wf = await _phase1_resolve_intent(
            msg, phone, wa_id, user, db,
            extraction_engine, intent_resolver, payload_factory, idempotency
        )

        if intent is None:
            # Duplicate message or skip signal
            return

        # === PHASE 2: Atomic Dispatch ===
        try:
            response, idem_key, msg_id = await _phase2_atomic_dispatch(
                phone, wa_id, db, trace_id,
                intent, payload, extraction,
                idempotency, state_machine
            )
        except Exception as e:
            db.rollback()
            db.expire_all()
            _mark_failed_after_rollback(db, getattr(e, "idem_key", None))
            logger.error(f"Atomic Section Failure: {e}", exc_info=True)
            await safe_fallback(phone, wa_id=wa_id)
            return

        # === PHASE 3: Send Response ===
        await _phase3_send_response(
            phone, wa_id, response, idem_key, msg_id, db, recovery, idempotency
        )

        await event_bus.emit_async({
            "event": intent.value,
            "source": extraction.source,
            "phone": phone,
            "pii_redact": True,
        })

    except Exception as e:
        db.rollback()
        db.expire_all()
        logger.error(f"Webhook outer failure: {e}", exc_info=True)
        if phone:
            await safe_fallback(phone, wa_id=wa_id)


@router.post("")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    messages = _extract_messages(body)
    for message in messages:
        await _process_message(message, db)
    return {"status": "ok"}
