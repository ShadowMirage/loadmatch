import asyncio
import dataclasses
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

from app.contracts.enums import Intent
from app.models.processed_message import ProcessedMessage, WorkflowEvent
from app.services.payload_factory import PayloadFactory
from app.services.dispatcher_service import DispatcherService
from app.services.recovery_service import RecoveryService
from app.services.session_manager import peek_session
from app.core.trace_context import set_trace_id
from app.core.recovery_utils import is_zombie, get_instance_id

def to_dict(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return to_dict(obj.__dict__)
    return obj

logger = logging.getLogger("loadmatch.recovery")

class RecoveryDaemon:
    def __init__(self, session_factory, check_interval: int = 30, zombie_threshold: int = 300, max_retries: int = 3):
        self.session_factory = session_factory
        self.check_interval = check_interval
        self.zombie_threshold = zombie_threshold
        self.max_retries = max_retries

    async def run_forever(self):
        logger.info(f"🚀 RecoveryDaemon started. Instance: {get_instance_id()}")
        while True:
            try:
                # 1. Reclaim stuck executions (Must-Fix)
                await self.scan_and_reclaim()
                
                # 2. Replay zombie/failed tasks
                await self.scan_and_replay()
                
                # 3. Deliver successful but undelivered tasks
                await self.scan_and_deliver()
            except Exception as e:
                logger.error(f"Recovery mapping loop failure: {e}", exc_info=True)
            await asyncio.sleep(self.check_interval)

    async def scan_and_reclaim(self):
        """
        Reclaims jobs stuck in EXECUTING state for > 5 minutes.
        Handles cases where a worker crashes AFTER claiming but BEFORE commit.
        """
        db = self.session_factory()
        try:
            now = datetime.now(timezone.utc)
            timeout = now - timedelta(minutes=5)
            
            stuck_jobs = (
                db.query(ProcessedMessage)
                .filter(
                    ProcessedMessage.status == "EXECUTING", # Specifically stuck ones
                    ProcessedMessage.execution_owner != None,
                    ProcessedMessage.execution_started_at < timeout
                )
                .with_for_update(skip_locked=True)
                .all()
            )
            
            for job in stuck_jobs:
                logger.warning(f"Reclaiming stuck job {job.idempotency_key} from owner {job.execution_owner}")
                job.status = "IN_PROGRESS" # Reset to allow regular scan to pick it up
                job.execution_owner = None
                job.execution_started_at = None
                
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Error in scan_and_reclaim: {e}")
        finally:
            db.close()

    async def scan_and_replay(self):
        db: Session = self.session_factory()
        try:
            now = datetime.now(timezone.utc)
            
            # 1. Atomic Arbitration Scanner (Must-Fix A)
            # Find jobs that are zombies or failed, but NOT currently being executed by another worker.
            # Using SKIP LOCKED for horizontal scale safety.
            candidates = (
                db.query(ProcessedMessage)
                .filter(
                    (ProcessedMessage.status.in_(["IN_PROGRESS", "FAILED"])) &
                    (
                        (ProcessedMessage.recovery_attempted_at == None) |
                        (ProcessedMessage.recovery_attempted_at < now - timedelta(seconds=30))
                    )
                )
                .with_for_update(skip_locked=True)
                .limit(50)
                .all()
            )

            if not candidates:
                return

            logger.info(f"🔍 RecoveryDaemon: Claimed {len(candidates)} replay candidates for execution.")

            for record in candidates:
                next_attempt = (record.retry_count or 0) + 1
                if next_attempt > self.max_retries:
                    self._move_to_dead_letter(
                        db,
                        record,
                        f"Retry limit reached ({record.retry_count or 0}/{self.max_retries}) before replay",
                    )
                    continue

                # Double-check zombie contract before actual work
                if record.status == "IN_PROGRESS" and not is_zombie(record):
                    continue
                
                # 2. Replay Ownership (Maturity logic)
                record.status = "EXECUTING"
                record.execution_owner = get_instance_id()
                record.execution_started_at = now
                record.recovery_attempted_at = now
                await self._replay_record(db, record)

        finally:
            db.close()

    async def _replay_record(self, db: Session, record: ProcessedMessage):
        trace_id = record.trace_id or f"replay-{record.id}"
        set_trace_id(trace_id)
        
        db.add(WorkflowEvent(
            processed_message_id=record.id,
            event_type="REPLAY_STARTED",
            trace_id=trace_id,
            payload={"owner": str(get_instance_id()), "trace": trace_id}
        ))

        logger.info(f"🔄 Replaying record {record.idempotency_key} (trace={trace_id})")

        # Parse key for user_id
        parts = record.idempotency_key.split(":")
        
        try:
            if not record.request_payload:
                raise ValueError("Missing request_payload during recovery")

            req = record.request_payload
            intent_val = req.get("intent")
            phone = req.get("phone")
            current_workflow = req.get("current_workflow")

            if not current_workflow and phone:
                session = peek_session(db, phone)
                current_workflow = session.current_workflow if session else None

            intent = Intent(intent_val)
            factory = PayloadFactory()
            replay_data = factory.extract_replay_data(req)
            reconstructed_payload = factory.build(intent, replay_data)

            dispatcher = DispatcherService(db, parts[0], phone=phone)
            response = dispatcher.execute(
                intent,
                reconstructed_payload,
                current_workflow=current_workflow,
            )

            record.status = "SUCCESS"
            record.workflow_step = record.workflow_step or "COMPLETED"
            record.delivery_state = "READY"
            record.response_payload = to_dict(response)
            record.execution_owner = None
            if getattr(record, "execution_started_at", None):
                started_at = record.execution_started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                record.execution_duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

            db.add(WorkflowEvent(
                processed_message_id=record.id,
                event_type="REPLAY_SUCCESS",
                trace_id=trace_id,
                payload={"at": datetime.now(timezone.utc).isoformat()}
            ))
            db.commit()
            logger.info(f"✅ Replay successful for {record.id}")

        except Exception as e:
            logger.error(f"Replay failed for {record.id}: {e}")
            db.rollback()
            self._record_replay_failure(db, record.id, trace_id, str(e))

    def _record_replay_failure(self, db: Session, record_id, trace_id: str, error_message: str) -> None:
        record = db.query(ProcessedMessage).filter(ProcessedMessage.id == record_id).first()
        if not record:
            return

        now = datetime.now(timezone.utc)
        next_retry_count = (record.retry_count or 0) + 1
        record.retry_count = next_retry_count
        record.execution_owner = None
        record.recovery_attempted_at = now
        record.failed_at = now
        record.error_log = {"error": error_message, "at": now.isoformat()}
        record.delivery_state = "FAILED"

        event_type = "REPLAY_FAILED"
        if next_retry_count >= self.max_retries:
            record.status = "DLQ"
            record.workflow_step = "DLQ"
            event_type = "REPLAY_DLQ"
        else:
            record.status = "FAILED"
            record.workflow_step = "FAILED"

        db.add(WorkflowEvent(
            processed_message_id=record.id,
            event_type=event_type,
            trace_id=trace_id,
            payload={"error": error_message, "retry_count": next_retry_count},
        ))
        db.commit()

    def _move_to_dead_letter(self, db: Session, record: ProcessedMessage, reason: str) -> None:
        now = datetime.now(timezone.utc)
        record.status = "DLQ"
        record.workflow_step = "DLQ"
        record.delivery_state = "FAILED"
        record.execution_owner = None
        record.failed_at = now
        record.error_log = {"error": reason, "at": now.isoformat()}
        db.add(WorkflowEvent(
            processed_message_id=record.id,
            event_type="REPLAY_DLQ",
            trace_id=record.trace_id,
            payload={"error": reason, "retry_count": record.retry_count or 0},
        ))
        db.commit()

    async def scan_and_deliver(self):
        """
        Maturity Pillar 3: Background Delivery recovery.
        Scanning for SUCCESSful execution where delivery hasn't happened.
        """
        db = self.session_factory()
        try:
            # Atomic delivery claim
            undelivered = (
                db.query(ProcessedMessage)
                .filter(
                    ProcessedMessage.status == "SUCCESS",
                    ProcessedMessage.delivered_at == None,
                    ProcessedMessage.response_payload != None
                )
                .with_for_update(skip_locked=True)
                .limit(20)
                .all()
            )
            
            if not undelivered:
                return
            
            recovery = RecoveryService(db)
            
            for record in undelivered:
                req = record.request_payload or {}
                phone = req.get("phone")
                if not phone:
                    continue
                
                logger.info(f"🚀 Recovering delivery for {record.idempotency_key}")
                success = await recovery.send_with_backoff(
                    phone,
                    record.response_payload,
                    ignore_guard=True,
                )
                if success:
                    record.delivered_at = datetime.now(timezone.utc)
                    record.delivery_state = "DELIVERED"
                    db.add(WorkflowEvent(
                        processed_message_id=record.id,
                        event_type="DELIVERY_SUCCESS",
                        trace_id=record.trace_id,
                        payload={"at": record.delivered_at.isoformat()}
                    ))
                    db.flush()
                    db.commit()
        finally:
            db.close()
