import logging
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.processed_message import ProcessedMessage

logger = logging.getLogger(__name__)


def is_duplicate(db: Session, wa_message_id: str) -> bool:
    """Return True if this WhatsApp message ID has already been recorded."""
    return db.query(ProcessedMessage).filter(
        ProcessedMessage.idempotency_key.like(f"%:{wa_message_id}:%")
    ).first() is not None


def mark_processed(db: Session, wa_message_id: str) -> bool:
    """
    Record a message ID as processed. Returns True on success, False if it
    was already recorded (i.e. a race condition caught by the unique constraint).
    """
    try:
        now = datetime.now(timezone.utc)
        record = ProcessedMessage(
            wamid=wa_message_id,
            idempotency_key=f"legacy:{wa_message_id}:processed",
            status="SUCCESS",
            workflow_step="DELIVERED",
            delivery_state="DELIVERED",
            expires_at=now + timedelta(days=7),
            replay_execution_hash=sha256(wa_message_id.encode("utf-8")).hexdigest(),
            request_payload={"wa_id": wa_message_id},
            response_payload={"status": "processed"},
            created_at=now,
            updated_at=now,
        )
        db.add(record)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        logger.warning("Duplicate wa_message_id detected by DB constraint: %s", wa_message_id)
        return False
    except Exception as e:
        db.rollback()
        logger.error("Error marking message as processed: %s", e)
        return False
