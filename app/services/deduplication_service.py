import logging
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.processed_message import ProcessedMessage

logger = logging.getLogger(__name__)


def is_duplicate(db: Session, wa_message_id: str) -> bool:
    """Return True if this wa_message_id has already been processed."""
    return db.query(ProcessedMessage).filter(
        ProcessedMessage.wa_message_id == wa_message_id
    ).first() is not None


def mark_processed(db: Session, wa_message_id: str) -> bool:
    """
    Record a message ID as processed. Returns True on success, False if it
    was already recorded (i.e. a race condition caught by the unique constraint).
    """
    try:
        record = ProcessedMessage(wa_message_id=wa_message_id)
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
