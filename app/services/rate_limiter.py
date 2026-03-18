import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.models.user_activity import UserActivity

logger = logging.getLogger(__name__)

RATE_LIMIT_MAX = 20          # messages
RATE_LIMIT_WINDOW_SECS = 60  # rolling window


def is_rate_limited(db: Session, user_id) -> bool:
    """
    Returns True if the user has exceeded RATE_LIMIT_MAX messages in the
    last RATE_LIMIT_WINDOW_SECS seconds. Also updates the rolling counter.
    """
    try:
        now = datetime.now(timezone.utc)
        activity = db.query(UserActivity).filter(UserActivity.user_id == user_id).first()

        if not activity:
            # First message ever — create record and allow
            activity = UserActivity(user_id=user_id, message_count=1, window_start=now)
            db.add(activity)
            db.commit()
            return False

        window_age = (now - activity.window_start.replace(tzinfo=timezone.utc)).total_seconds()

        if window_age > RATE_LIMIT_WINDOW_SECS:
            # Window expired — reset counter
            activity.window_start = now
            activity.message_count = 1
            db.commit()
            return False

        # Still inside the window
        activity.message_count += 1
        db.commit()

        if activity.message_count > RATE_LIMIT_MAX:
            logger.warning("Rate limit exceeded for user_id=%s count=%d", user_id, activity.message_count)
            return True

        return False

    except Exception as e:
        logger.error("Rate limiter error for user_id=%s: %s", user_id, e)
        db.rollback()
        return False  # Fail open — don't block users on limiter errors
