import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.user_activity import UserActivity
from app.runtime.redis_adapter import get_client as get_redis_client

logger = logging.getLogger(__name__)

RATE_LIMIT_MAX = 20          # messages
RATE_LIMIT_WINDOW_SECS = 60  # rolling window
INVALID_INPUTS = {"0 kg", "0kg", "0", "test", "nonsense"}

_user_message_history = {}


def _normalize_user_id(user_id):
    """
    Coerce UUID-like strings into UUID objects so SQLite/Postgres test paths
    behave the same as production model bindings.
    """
    if isinstance(user_id, uuid.UUID):
        return user_id
    if isinstance(user_id, str):
        try:
            return uuid.UUID(user_id)
        except ValueError:
            return user_id
    return user_id


def is_rate_limited(db: Session, user_id, commit: bool = False) -> bool:
    """
    Returns True if the user has exceeded RATE_LIMIT_MAX messages in the
    last RATE_LIMIT_WINDOW_SECS seconds. Also updates the rolling counter.
    """
    redis_client = get_redis_client()
    if redis_client is not None:
        try:
            key = f"loadmatch:rate_limit:{user_id}"
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, RATE_LIMIT_WINDOW_SECS)
            limited = count > RATE_LIMIT_MAX
            if limited:
                logger.warning("Rate limit exceeded for user_id=%s count=%d", user_id, count)
            return limited
        except Exception as exc:
            logger.warning("Redis rate limiter unavailable for user_id=%s: %s", user_id, exc)

    try:
        now = datetime.now(timezone.utc)
        normalized_user_id = _normalize_user_id(user_id)
        with db.begin_nested():
            activity = db.query(UserActivity).filter(UserActivity.user_id == normalized_user_id).first()

            if not activity:
                activity = UserActivity(user_id=normalized_user_id, message_count=1, window_start=now)
                db.add(activity)
                db.flush()
                limited = False
            else:
                window_age = (now - activity.window_start.replace(tzinfo=timezone.utc)).total_seconds()

                if window_age > RATE_LIMIT_WINDOW_SECS:
                    activity.window_start = now
                    activity.message_count = 1
                    limited = False
                else:
                    activity.message_count += 1
                    limited = activity.message_count > RATE_LIMIT_MAX

                db.flush()

        if commit:
            db.commit()

        if limited:
            logger.warning("Rate limit exceeded for user_id=%s count=%d", user_id, activity.message_count)
            return True

        return limited

    except Exception as e:
        logger.error("Rate limiter error for user_id=%s: %s", user_id, e)
        return False  # Fail open — don't block users on limiter errors


def check(db: Session, user_id, input_text: str = "", commit: bool = False) -> bool:
    cleaned_input = str(input_text or "").strip().lower()
    if cleaned_input in INVALID_INPUTS:
        return True

    if is_rate_limited(db, user_id, commit=commit):
        return True

    if not cleaned_input:
        return False

    history_key = str(user_id)
    now = time.time()
    history = _user_message_history.get(history_key, {"timestamps": [], "last_text": "", "repeat_count": 0})

    if cleaned_input == history["last_text"] and len(cleaned_input) > 4:
        history["repeat_count"] += 1
        history["timestamps"].append(now)
        _user_message_history[history_key] = history
        return history["repeat_count"] >= 2

    valid_times = [timestamp for timestamp in history["timestamps"] if now - timestamp <= 10]
    valid_times.append(now)
    history["timestamps"] = valid_times
    history["last_text"] = cleaned_input
    history["repeat_count"] = 0
    _user_message_history[history_key] = history
    return len(valid_times) > 5
    
_spam_cache = {}

def clear_spam_history(user_id=None):
    if user_id:
        _spam_cache.pop(user_id, None)
    else:
        _spam_cache.clear()