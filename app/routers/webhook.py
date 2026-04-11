import logging
import dataclasses
import time
import re
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
from app.services.date_parser import normalize_date
from app.services.logistics_data import RESOLVER_VERSION
from app.services.rate_limiter import check as rate_limit_check
from app.services.whatsapp_service import send_text, safe_fallback
from app.models.processed_message import ProcessedMessage, WorkflowEvent

# ✅ SINGLE SOURCE OF TRUTH
from app.contracts.responses import Response as ContractResponse
from app.contracts.enums import Intent

logger = logging.getLogger("loadmatch.webhook")
router = APIRouter(prefix="/webhook", tags=["Webhook"])
_TRUCK_PLATE_PATTERN = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$")
_DATE_SEARCH_PATTERNS = (
    re.compile(r"\b(today|tomorrow)\b", flags=re.IGNORECASE),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b"),
)


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


def _parse_quantity_kg_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    normalized = str(text).strip().lower()
    ton_match = re.search(r"(\d+\.?\d*)\s*(?:t|ton|tons)\b", normalized, flags=re.IGNORECASE)
    if ton_match:
        return int(float(ton_match.group(1)) * 1000)
    kg_match = re.search(r"(\d+\.?\d*)\s*(?:kg|kgs|kilo|kilogram|kilograms)\b", normalized, flags=re.IGNORECASE)
    if kg_match:
        return int(float(kg_match.group(1)))
    return None


def _normalize_date_value(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().strftime("%d-%m-%Y")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")

    normalized = normalize_date(str(value).strip())
    return normalized or None


def _parse_date_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    raw_text = str(text).strip()
    for pattern in _DATE_SEARCH_PATTERNS:
        match = pattern.search(raw_text)
        if match:
            normalized = _normalize_date_value(match.group(0))
            if normalized:
                return normalized
    return None


def _looks_like_truck_plate(value: Any) -> bool:
    if value in (None, ""):
        return False
    normalized = re.sub(r"\s+", "", str(value).upper())
    return bool(_TRUCK_PLATE_PATTERN.fullmatch(normalized))


def _sanitize_plate_alias(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}

    plate_value = data.get("plate")
    if plate_value in (None, ""):
        return data

    normalized_plate_date = _normalize_date_value(plate_value)
    if normalized_plate_date and not _looks_like_truck_plate(plate_value):
        data.setdefault("date", normalized_plate_date)
        data.pop("plate", None)
        return data

    if data.get("date") and not _looks_like_truck_plate(plate_value):
        data.pop("plate", None)

    return data


def _canonicalize_workflow_slots(data: dict, intent: Intent) -> dict:
    if not isinstance(data, dict):
        return {}

    normalized = dict(data)
    normalized = _sanitize_plate_alias(normalized)

    for date_key in ("date", "pickup_date", "departure_date"):
        normalized_date = _normalize_date_value(normalized.get(date_key))
        if normalized_date:
            normalized[date_key] = normalized_date

    if intent == Intent.CREATE_LOAD:
        if normalized.get("capacity_kg") and not normalized.get("weight_kg"):
            normalized["weight_kg"] = normalized["capacity_kg"]
        if normalized.get("date") and not normalized.get("pickup_date"):
            normalized["pickup_date"] = normalized["date"]
        normalized.pop("departure_date", None)
        normalized.pop("date", None)
    elif intent == Intent.POST_TRUCK:
        if normalized.get("from_city") and not normalized.get("current_city"):
            normalized["current_city"] = normalized["from_city"]
        if normalized.get("weight_kg") and not normalized.get("capacity_kg"):
            normalized["capacity_kg"] = normalized["weight_kg"]
        if normalized.get("date") and not normalized.get("departure_date"):
            normalized["departure_date"] = normalized["date"]
        normalized.pop("pickup_date", None)
        normalized.pop("date", None)

    normalized.pop("weight", None)
    normalized.pop("capacity", None)
    return normalized


def _workflow_family(workflow: Optional[str]) -> Optional[str]:
    normalized = str(workflow or "").upper()
    if normalized.startswith("LOAD_"):
        return "load"
    if normalized.startswith("TRUCK_"):
        return "truck"
    return None


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


def clear_session(db: Session, phone: str) -> None:
    StateMachineService.clear_session_and_metadata(db, phone)


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
    trace_id: str = "",
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
    interrupt_menu_active = bool(session_data.get("interrupt_menu_active"))
    current_workflow = None

    stored_workflow = getattr(session, "current_workflow", None)
    if stored_workflow is None:
        stored_workflow = getattr(user, "state", None)

    reconstructed_workflow = StateMachineService.reconstruct_workflow(stored_workflow, session_data)
    reconstruction_aborted = (
        bool(stored_workflow)
        and StateMachineService.workflow_is_active(stored_workflow)
        and not reconstructed_workflow
        and not StateMachineService.session_has_workflow_slots(session_data)
        and not StateMachineService.session_has_workflow_anchors(session_data)
    )
    if reconstruction_aborted:
        logger.warning(
            "[SESSION_RECONSTRUCTION_ABORT_RECOVERY]",
            extra={"phone": phone, "stored_workflow": stored_workflow},
        )
        clear_session(db, phone)
        user.state = "IDLE"
        session_data = {}
        interrupt_menu_active = False
        current_workflow = "IDLE"
    else:
        if not reconstructed_workflow and StateMachineService.should_reconstruct_from_session(stored_workflow, session_data):
            reconstructed_workflow = StateMachineService.reconstruct_workflow_from_session_data(
                session_data,
                preferred_workflow=stored_workflow,
            )
            if reconstructed_workflow:
                logger.warning(
                    "[LIFECYCLE_INVARIANT_VIOLATION]",
                    extra={
                        "phone": phone,
                        "stored_workflow": stored_workflow,
                        "reconstructed_workflow": reconstructed_workflow,
                    },
                )
        if reconstructed_workflow:
            current_workflow = reconstructed_workflow
        elif stored_workflow and StateMachineService.workflow_is_active(stored_workflow):
            session_data.clear()

    # Phase-1 workflow authority sync guard:
    # normalize non-active workflow markers to IDLE and align with TTL expiry
    # before resolver intent selection.
    if current_workflow and not StateMachineService.workflow_is_active(current_workflow):
        current_workflow = "IDLE"
        user.state = "IDLE"
    elif current_workflow and StateMachineService.workflow_is_active(current_workflow):
        workflow_updated_at = getattr(session, "updated_at", None) or getattr(user, "updated_at", None)
        if isinstance(workflow_updated_at, datetime):
            normalized_updated_at = (
                workflow_updated_at.replace(tzinfo=timezone.utc)
                if workflow_updated_at.tzinfo is None
                else workflow_updated_at.astimezone(timezone.utc)
            )
            if datetime.now(timezone.utc) - normalized_updated_at > StateMachineService.SESSION_TTL:
                logger.info(
                    "[PHASE1_WORKFLOW_EXPIRED] workflow=%s phone=%s",
                    current_workflow,
                    phone,
                )
                current_workflow = "IDLE"
                user.state = "IDLE"

    current_workflow = current_workflow or "IDLE"
    session_data_for_extraction = dict(session_data)
    session_data_for_extraction["current_workflow"] = current_workflow

    extraction = await extraction_engine.extract(raw_text, user, session_data_for_extraction)
    logger.info(f"[EXTRACTION] intent={extraction.intent} fresh_data={extraction.data}")

    # Quantity recovery guard immediately after extraction.
    extraction_data = extraction.data if isinstance(extraction.data, dict) else {}
    interactive_action_id = ""
    if interactive_payload:
        interactive_action_id = str(interactive_payload.get("id") or "").strip()
        interactive_action_title = str(interactive_payload.get("title") or "").strip()
        if interactive_action_id:
            extraction_data = {
                **extraction_data,
                "interactive_action_id": interactive_action_id,
                "interactive_action_title": interactive_action_title,
            }
    parsed_qty_kg = _parse_quantity_kg_from_text(raw_text)
    if parsed_qty_kg:
        extraction_data.setdefault("weight_kg", parsed_qty_kg)
        extraction_data.setdefault("capacity_kg", parsed_qty_kg)
    parsed_date = _parse_date_from_text(raw_text)
    if parsed_date and not any(extraction_data.get(key) for key in ("date", "pickup_date", "departure_date")):
        extraction_data["date"] = parsed_date
    extraction_data = _sanitize_plate_alias(extraction_data)
    extraction.data = extraction_data

    # Support partial updates (corrections): merge prior session_data with fresh extraction.
    effective_session_data = session_data if isinstance(session_data, dict) else {}
    interaction_data = {}
    if interactive_payload:
        interaction_data = {
            "interactive_action_id": interactive_payload.get("id"),
            "interactive_action_title": interactive_payload.get("title"),
        }
    merged_data = {**effective_session_data, **interaction_data, **extraction_data}

    # Normalize aliases BEFORE intent resolution and payload factory.
    raw_weight = merged_data.get("weight")
    if raw_weight and isinstance(raw_weight, str) and "ton" in raw_weight.lower():
        m = re.search(r'([\d.]+)', raw_weight)
        if m:
            merged_data["weight_kg"] = int(float(m.group(1)) * 1000)
    elif raw_weight:
        merged_data["weight_kg"] = raw_weight

    raw_cap = merged_data.get("capacity")
    if raw_cap and isinstance(raw_cap, str) and "ton" in raw_cap.lower():
        m = re.search(r'([\d.]+)', raw_cap)
        if m:
            merged_data["capacity_kg"] = int(float(m.group(1)) * 1000)
    elif raw_cap:
        merged_data["capacity_kg"] = raw_cap

    # Raw-text quantity fallback when extraction/session alias fields are missing.
    if not merged_data.get("weight_kg") and not merged_data.get("capacity_kg"):
        qty_kg = _parse_quantity_kg_from_text(raw_text)
        if qty_kg:
            merged_data["weight_kg"] = qty_kg
            merged_data["capacity_kg"] = qty_kg

    for date_key in ("date", "pickup_date", "departure_date"):
        normalized_date = _normalize_date_value(merged_data.get(date_key))
        if normalized_date:
            merged_data[date_key] = normalized_date

    if not any(merged_data.get(key) for key in ("date", "pickup_date", "departure_date")):
        parsed_date = _parse_date_from_text(raw_text)
        if parsed_date:
            merged_data["date"] = parsed_date

    merged_data = _sanitize_plate_alias(merged_data)

    if not merged_data.get("resolver_version"):
        merged_data["resolver_version"] = effective_session_data.get("resolver_version") or RESOLVER_VERSION

    extraction.data = merged_data
    intent = intent_resolver.resolve(
        extraction,
        interactive_payload,
        current_workflow,
        message_text=raw_text,
        interrupt_menu_active=interrupt_menu_active,
    )

    interactive_action_id_norm = str(merged_data.get("interactive_action_id") or "").upper()
    if interactive_action_id_norm in {"POST_TRUCK", "START_TRUCK"}:
        intent = Intent.POST_TRUCK
    elif interactive_action_id_norm in {"FIND_TRUCK", "POST_LOAD"}:
        intent = Intent.CREATE_LOAD

    workflow_family = _workflow_family(current_workflow)

    # Workflow-aware intent correction guard.
    if workflow_family == "truck" and intent == Intent.CREATE_LOAD:
        intent = Intent.POST_TRUCK
    if workflow_family == "load" and intent == Intent.POST_TRUCK:
        intent = Intent.CREATE_LOAD

    has_route = bool(merged_data.get("from_city")) and bool(merged_data.get("to_city"))
    has_weight = bool(merged_data.get("weight_kg"))
    has_capacity = bool(merged_data.get("capacity_kg"))

    if (
        intent in {Intent.CREATE_LOAD, Intent.POST_TRUCK}
        and has_route
        and not (has_weight or has_capacity)
        and current_workflow == "IDLE"
    ):
        intent = Intent.UNKNOWN

    # Route-only promotion protection.
    if intent == Intent.UNKNOWN and has_route:
        if not (has_weight or has_capacity):
            intent = Intent.UNKNOWN
        elif workflow_family == "truck":
            intent = Intent.POST_TRUCK
        elif workflow_family == "load":
            intent = Intent.CREATE_LOAD
        elif has_capacity and not has_weight:
            intent = Intent.POST_TRUCK
        else:
            intent = Intent.CREATE_LOAD

    # Session continuity safety net when extractor fails.
    if intent == Intent.UNKNOWN and effective_session_data:
        if effective_session_data.get("from_city") and effective_session_data.get("to_city"):
            intent = Intent.CREATE_LOAD

    # Final payload boundary canonicalization gate.
    merged_data = _canonicalize_workflow_slots(merged_data, intent)
    if not merged_data.get("resolver_version"):
        merged_data["resolver_version"] = RESOLVER_VERSION

    extraction.data = merged_data
    logger.info(f"[MERGED_DATA] intent={intent} merged_data={merged_data}")

    # Attempt payload construction. If it fails (missing fields), we don't crash.
    # Interrupt intents (GREETING, MENU, UNKNOWN) carry no domain payload — skip build.
    payload = None
    if intent not in INTERRUPT_INTENTS:
        try:
            payload = payload_factory.build(intent, merged_data)
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

    # Build merged authority surface before transition guards.
    persisted_session = peek_session(db, phone)
    persisted_session_data = get_session_data(db, phone, locked_user.id, create=False)
    persisted_session_data = persisted_session_data if isinstance(persisted_session_data, dict) else {}
    merge_dispatcher = DispatcherService(db, locked_user.id, phone=phone)
    merged_data = merge_dispatcher._collect_payload_data(payload)
    if isinstance(extraction.data, dict):
        merged_data = {**merged_data, **extraction.data}

    if StateMachineService.should_reconstruct_from_session(current_db_state, persisted_session_data):
        reconstructed_workflow = StateMachineService.reconstruct_workflow_from_session_data(
            merged_data,
            preferred_workflow=getattr(persisted_session, "current_workflow", None),
        )
        if reconstructed_workflow:
            logger.warning(
                "[LIFECYCLE_INVARIANT_VIOLATION]",
                extra={
                    "phone": phone,
                    "stored_state": current_db_state,
                    "reconstructed_workflow": reconstructed_workflow,
                },
            )
            current_db_state = reconstructed_workflow
            locked_user.state = reconstructed_workflow

    transition_reference_time = locked_user.updated_at or datetime.now(timezone.utc)
    session_updated_at = getattr(persisted_session, "updated_at", None)
    if (
        isinstance(session_updated_at, datetime)
        and (
            StateMachineService.session_has_workflow_slots(merged_data)
            or StateMachineService.session_has_workflow_anchors(merged_data)
        )
        and session_updated_at > transition_reference_time
    ):
        transition_reference_time = session_updated_at

    transition = state_machine.transition(
        current_db_state,
        intent,
        transition_reference_time,
    )

    if intent not in INTERRUPT_INTENTS and not transition.allowed:
        logger.warning(f"Transition denied: {transition.error_message}")
        response = ContractResponse(text=transition.error_message or "⚠️ Action not allowed.")
        # Even on denial, we might want to save data if it was a correction attempt
    else:
        if intent == Intent.CANCEL:
            locked_user.state = "IDLE"

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
    workflow_expired = bool(getattr(transition, "workflow_expired", False))

    interrupt_menu_open = (
        StateMachineService.workflow_is_active(next_session_workflow)
        and intent in {Intent.GREETING, Intent.UNKNOWN, Intent.MENU}
    )
    if isinstance(extraction.data, dict):
        extraction.data["interrupt_menu_active"] = interrupt_menu_open

    # Preserve slot state only while a workflow remains active. IDLE transitions
    # must clear the session buffer, otherwise cancel/confirm paths repopulate
    # stale payload data and the next workflow resumes with old slots.
    if not StateMachineService.workflow_is_active(next_session_workflow):
        if workflow_expired and intent not in {Intent.CANCEL, Intent.CONFIRM}:
            # TTL should expire workflow state, not erase collected slots.
            get_or_create_session(db, phone, locked_user.id)
            if isinstance(extraction.data, dict):
                set_session_data(db, phone, locked_user.id, extraction.data)
            update_session(
                db,
                phone,
                {"current_workflow": None},
                commit=False,
            )
        else:
            terminal_session_data = get_session_data(db, phone, locked_user.id, create=False)
            if terminal_session_data:
                StateMachineService.cleanup_terminal_state(terminal_session_data)
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
            extraction_engine, intent_resolver, payload_factory, idempotency,
            trace_id=trace_id,
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
