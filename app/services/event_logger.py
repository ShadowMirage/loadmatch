import logging
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.models.event import EventLog

logger = logging.getLogger(__name__)

def track_event(db: Session, user_id, event_type: str, data: dict = None):
    """
    Centralized event logger that safely records events to the database without breaking workflows.
    """
    try:
        event = EventLog(
            user_id=user_id,
            event_type=event_type,
            data=data or {}
        )
        db.add(event)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log event {event_type} for user {user_id}: {e}")
        db.rollback()
