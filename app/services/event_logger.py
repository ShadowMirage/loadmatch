import logging
from sqlalchemy.orm import Session

from app.models.event import EventLog

logger = logging.getLogger(__name__)


def track_event(db: Session, user_id, event_type: str, data: dict = None, commit: bool = False):
    """
    Centralized event logger that safely records events to the database without breaking workflows.
    """
    try:
        with db.begin_nested():
            event = EventLog(
                user_id=user_id,
                event_type=event_type,
                data=data or {}
            )
            db.add(event)
            db.flush()
        if commit:
            db.commit()
        return event
    except Exception as e:
        logger.error(f"Failed to log event {event_type} for user {user_id}: {e}")
        return None
