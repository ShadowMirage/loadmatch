import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.processed_message import ProcessedMessage
from app.contracts.enums import Intent
from app.services.payload_factory import PayloadFactory


class IdempotencyService:
    def __init__(self, db: Session, ttl_seconds: int = 30):
        self.db = db
        self.ttl_seconds = ttl_seconds

    def exists(self, wa_id: str) -> bool:
        """Exactly-once guard keyed by wamid, with legacy fallback support."""
        return self.find_record(wa_id) is not None

    def find_record(self, wa_id: str) -> Optional[ProcessedMessage]:
        """Exposed method to retrieve a record by wamid or idempotency key fragment."""
        return (
            self.db.query(ProcessedMessage)
            .filter(
                or_(
                    ProcessedMessage.wamid == wa_id,
                    ProcessedMessage.idempotency_key.like(f"%:{wa_id}:%"),
                )
            )
            .first()
        )

    def fetch_cached_response(self, wa_id: str) -> Optional[Any]:
        """Retrieves cached response_payload if the message was already successfully processed."""
        record = self.find_record(wa_id)
        if record and record.status == "SUCCESS" and record.response_payload:
            import logging
            logging.getLogger(__name__).info(f"Returning cached response for duplicate wamid: {wa_id}")
            return record.response_payload
        return None

    def fetch_cached_intent_data(self, wa_id: str) -> Optional[tuple[Intent, dict]]:
        """Retrieves replay-safe intent/extraction data for a completed duplicate only."""
        record = self.find_record(wa_id)
        if not record or record.status != "SUCCESS":
            return None

        from app.contracts.enums import Intent
        try:
            intent_val = Intent(record.intent)
        except (ValueError, TypeError):
            intent_val = Intent.UNKNOWN

        extraction_data = PayloadFactory.extract_replay_data(record.request_payload)
        return intent_val, extraction_data

    def start(
        self,
        key: str,
        trace_id: str,
        request_payload: Optional[dict] = None,
        *,
        wamid: Optional[str] = None,
        user_id: Optional[Any] = None,
        intent: Optional[str] = None,
        confidence: Optional[float | int] = None,
        workflow_step: Optional[str] = None,
        dispatcher_action: Optional[str] = None,
        delivery_state: Optional[str] = None,
    ) -> Optional[ProcessedMessage]:
        """
        Marks a task as IN_PROGRESS with trace-preserving self-healing.
        Returns the ProcessedMessage object if started/restarted, else None.
        """
        now = datetime.now(timezone.utc)
        canonical_wamid = self._derive_wamid(key, wamid, request_payload)
        execution_hash = self._build_replay_hash(canonical_wamid, key, user_id, request_payload)

        existing = (
            self.db.query(ProcessedMessage)
            .filter(
                or_(
                    ProcessedMessage.idempotency_key == key,
                    ProcessedMessage.wamid == canonical_wamid,
                )
            )
            .with_for_update()
            .first()
        )

        if existing:
            if existing.status == "SUCCESS":
                return None

            if existing.status in ("IN_PROGRESS", "EXECUTING", "FAILED", "REPLAYING"):
                last_updated = existing.updated_at or existing.created_at or now
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=timezone.utc)
                age = now - last_updated

                if existing.status in ("IN_PROGRESS", "EXECUTING") and age <= timedelta(seconds=self.ttl_seconds):
                    return None

                existing.status = "IN_PROGRESS"
                existing.trace_id = trace_id
                existing.updated_at = now
                existing.request_payload = request_payload
                existing.retry_count = (existing.retry_count or 0) + 1
                existing.user_id = user_id or existing.user_id
                existing.wamid = canonical_wamid
                existing.intent = intent or existing.intent
                existing.confidence = self._normalize_confidence(confidence, existing.confidence)
                existing.workflow_step = workflow_step or existing.workflow_step
                existing.dispatcher_action = dispatcher_action or existing.dispatcher_action
                existing.delivery_state = delivery_state or existing.delivery_state or "PENDING"
                existing.dispatch_started_at = now
                existing.replay_execution_hash = execution_hash
                self.db.flush()
                return existing

        try:
            with self.db.begin_nested():
                new_entry = ProcessedMessage(
                    wamid=canonical_wamid,
                    user_id=user_id,
                    idempotency_key=key,
                    trace_id=trace_id,
                    intent=intent,
                    confidence=self._normalize_confidence(confidence),
                    workflow_step=workflow_step,
                    dispatcher_action=dispatcher_action or intent,
                    delivery_state=delivery_state or "PENDING",
                    status="IN_PROGRESS",
                    retry_count=0,
                    dispatch_started_at=now,
                    replay_execution_hash=execution_hash,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(days=7),
                    request_payload=request_payload,
                )
                self.db.add(new_entry)
                self.db.flush()
            return new_entry
        except IntegrityError:
            fallback_entry = (
                self.db.query(ProcessedMessage)
                .filter(
                    or_(
                        ProcessedMessage.idempotency_key == key,
                        ProcessedMessage.wamid == canonical_wamid,
                    )
                )
                .with_for_update()
                .first()
            )
            return fallback_entry

    def complete(self, key: str, response_payload: Any):
        """Marks a task as SUCCESS and caches the response."""
        entry = (
            self.db.query(ProcessedMessage)
            .filter(ProcessedMessage.idempotency_key == key)
            .with_for_update()
            .first()
        )

        if entry:
            entry.status = "SUCCESS"
            entry.workflow_step = entry.workflow_step or "COMPLETED"
            entry.delivery_state = "READY"
            entry.response_payload = response_payload
            if entry.dispatch_started_at:
                started_at = entry.dispatch_started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                entry.execution_duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            self.db.flush()

    def mark_failed(self, key: str):
        """Marks a task as FAILED, allowing daemon-driven retry."""
        entry = (
            self.db.query(ProcessedMessage)
            .filter(ProcessedMessage.idempotency_key == key)
            .with_for_update()
            .first()
        )

        if entry:
            entry.status = "FAILED"
            entry.workflow_step = "FAILED"
            entry.delivery_state = "FAILED"
            entry.failed_at = datetime.now(timezone.utc)
            entry.retry_count = (entry.retry_count or 0) + 1
            self.db.flush()

    def mark_dead_letter(self, key: str, error: Any):
        """Marks a task as DLQ, isolating poison payloads."""
        entry = (
            self.db.query(ProcessedMessage)
            .filter(ProcessedMessage.idempotency_key == key)
            .with_for_update()
            .first()
        )

        if entry:
            entry.status = "DLQ"
            entry.workflow_step = "DLQ"
            entry.delivery_state = "FAILED"
            entry.error_log = {"error": str(error), "timestamp": datetime.now(timezone.utc).isoformat()}
            self.db.flush()

    def mark_delivered(self, key: str):
        """Marks a task as delivered."""
        entry = (
            self.db.query(ProcessedMessage)
            .filter(ProcessedMessage.idempotency_key == key)
            .with_for_update()
            .first()
        )

        if entry:
            entry.delivered_at = datetime.now(timezone.utc)
            entry.delivery_state = "DELIVERED"
            entry.workflow_step = "DELIVERED"
            self.db.flush()

    def get_cached_response(self, key: str) -> Optional[Any]:
        entry = (
            self.db.query(ProcessedMessage)
            .filter(
                ProcessedMessage.idempotency_key == key,
                ProcessedMessage.status == "SUCCESS",
            )
            .first()
        )
        return entry.response_payload if entry else None

    def execute(self, key: str, trace_id: str, fn: Callable[[], Any], force: bool = False) -> Any:
        if not force:
            cached = self.get_cached_response(key)
            if cached:
                return cached

        if not self.start(key, trace_id):
            cached = self.get_cached_response(key)
            if cached:
                return cached
            raise RuntimeError(f"Task {key} is already in progress or failed to start.")

        result = fn()
        self.complete(key, result)
        return result

    @staticmethod
    def _normalize_confidence(value: Optional[float | int], default: Optional[int] = None) -> Optional[int]:
        if value is None:
            return default
        if isinstance(value, int):
            return value if value > 1 else value * 100
        return int(round(float(value) * 100)) if float(value) <= 1 else int(round(float(value)))

    @staticmethod
    def _derive_wamid(key: str, wamid: Optional[str], request_payload: Optional[dict]) -> str:
        if wamid:
            return str(wamid)

        if isinstance(request_payload, dict):
            payload_wamid = request_payload.get("wa_id") or request_payload.get("wamid")
            if payload_wamid:
                return str(payload_wamid)

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return f"synthetic.{digest}"

    @staticmethod
    def _build_replay_hash(wamid: str, key: str, user_id: Optional[Any], request_payload: Optional[dict]) -> str:
        material = {
            "wamid": wamid,
            "idempotency_key": key,
            "user_id": str(user_id) if user_id is not None else None,
            "request_payload": request_payload or {},
        }
        encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
