from sqlalchemy.orm import Session
from app.models.user_session import UserSession
import json as _json


def get_session(db: Session, phone: str, user_id: str) -> UserSession:
    """
    Retrieve or create a session for a user.
    """
    user_session = db.query(UserSession).filter(UserSession.session_id == phone).first()

    if not user_session:
        user_session = UserSession(
            user_id=user_id,
            session_id=phone,
            current_workflow=None,
            step_number=0,
            expected_input=None
        )
        db.add(user_session)
        db.commit()
        db.refresh(user_session)

    return user_session


def get_session_data(db: Session, phone: str, user_id: str) -> dict:
    """
    Returns the session_data dict (parsed from JSON), or empty dict.
    """
    sess = get_session(db, phone, user_id)
    raw = getattr(sess, "session_data", None)
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return _json.loads(raw)
    except Exception:
        return {}


def update_session(db: Session, phone: str, updates: dict) -> UserSession:
    """
    Update session fields safely.
    Dict values are JSON-serialized (fixes silent drop bug).
    """
    user_session = db.query(UserSession).filter(UserSession.session_id == phone).first()

    if not user_session:
        return None

    for key, value in updates.items():
        if hasattr(user_session, key):
            if isinstance(value, dict):
                # Serialize dicts to JSON string instead of silently dropping
                setattr(user_session, key, _json.dumps(value))
            else:
                setattr(user_session, key, value)

    db.commit()
    db.refresh(user_session)

    return user_session


def clear_session(db: Session, phone: str) -> None:
    """
    Reset the session workflow and data.
    """
    user_session = db.query(UserSession).filter(UserSession.session_id == phone).first()

    if user_session:
        user_session.current_workflow = None
        user_session.step_number = 0
        user_session.expected_input = None
        user_session.session_data = None
        db.commit()


def set_session_data(db: Session, phone: str, user_id: str, data: dict) -> None:
    """
    Stores extraction data in session.
    FIXED: merges new data with existing session data (multi-message support)
    """
    sess = get_session(db, phone, user_id)

    # 🔥 STEP 1: Load existing data
    existing = {}
    raw_data = getattr(sess, "session_data", None)
    if raw_data:
        try:
            existing = _json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        except Exception:
            existing = {}

    # 🔥 STEP 2: Merge (NEW overwrites OLD keys only)
    merged = {**existing, **data}

    # 🔥 STEP 3: Save merged data
    sess.session_data = _json.dumps(merged)
    db.commit()


def merge_session_data(db: Session, phone: str, user_id: str, data: dict) -> dict:
    """
    Returns merged session data without saving (for preview usage)
    """
    existing = get_session_data(db, phone, user_id)
    return {**existing, **data}