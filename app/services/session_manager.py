from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.user_session import UserSession
import json as _json


def _session_query(db: Session, phone: str):
    return (
        db.query(UserSession)
        .filter(UserSession.session_id == phone)
        .order_by(UserSession.updated_at.desc(), UserSession.id.desc())
    )


def peek_session(db: Session, phone: str) -> UserSession | None:
    """
    Read-only session lookup.
    Never creates rows and always prefers the newest session if historical duplicates exist.
    """
    return _session_query(db, phone).first()


def get_or_create_session(db: Session, phone: str, user_id: str) -> UserSession:
    """
    Retrieve or create a session for a user.
    """
    user_session = peek_session(db, phone)

    if user_session:
        return user_session

    try:
        with db.begin_nested():
            user_session = UserSession(
                user_id=user_id,
                session_id=phone,
                current_workflow=None,
                step_number=0,
                expected_input=None,
            )
            db.add(user_session)
            db.flush()
            db.refresh(user_session)
    except IntegrityError:
        user_session = peek_session(db, phone)

    return user_session

def get_session_data(db: Session, phone: str, user_id: str, create: bool = True) -> dict:
    """
    Returns the session_data dict (parsed from JSON), or empty dict.
    """
    sess = get_or_create_session(db, phone, user_id) if create else peek_session(db, phone)
    if not sess:
        return {}
    raw = getattr(sess, "session_data", None)
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return _json.loads(raw)
    except Exception:
        return {}


def update_session(db: Session, phone: str, updates: dict, commit: bool = True) -> UserSession:
    """
    Update session fields safely.
    Dict values are JSON-serialized (fixes silent drop bug).
    """
    user_session = peek_session(db, phone)

    if not user_session:
        return None

    for key, value in updates.items():
        if hasattr(user_session, key):
            if isinstance(value, dict):
                # Serialize dicts to JSON string instead of silently dropping
                setattr(user_session, key, _json.dumps(value))
            else:
                setattr(user_session, key, value)

    if commit:
        db.flush()
        db.refresh(user_session)
    else:
        db.flush()

    return user_session


def clear_session(db: Session, phone: str) -> None:
    """
    Reset the session workflow and data.
    """
    user_session = peek_session(db, phone)

    if user_session:
        session_data = {}
        raw = getattr(user_session, "session_data", None)
        if isinstance(raw, dict):
            session_data = dict(raw)
        elif isinstance(raw, str):
            try:
                session_data = _json.loads(raw)
            except Exception:
                session_data = {}

        for field in (
            "lane_key",
            "directional_lane_key",
            "reverse_directional_lane_key",
            "lane_class",
            "corridor_detected",
            "confidence_source",
            "corridor_source",
            "resolver_version",
        ):
            session_data.pop(field, None)

        user_session.current_workflow = None
        user_session.step_number = 0
        user_session.expected_input = None
        user_session.session_data = None
        db.flush()


def set_session_data(db: Session, phone: str, user_id: str, data: dict) -> None:
    """
    Stores extraction data in session.
    FIXED: merges new data with existing session data (multi-message support)
    """
    # Always re-query to get an attached ORM object (avoids detached-instance issues)
    sess = get_or_create_session(db, phone, user_id)

    # 🔥 STEP 1: Load existing data
    existing = {}
    raw_data = getattr(sess, "session_data", None)
    if raw_data:
        try:
            existing = _json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        except Exception:
            existing = {}

    # 🔥 STEP 2: Merge (NEW overwrites OLD keys only)
    merged = {**existing, **(data or {})}

    # 🔥 STEP 3: Save merged data without committing
    sess.session_data = _json.dumps(merged)
    db.flush()


def merge_session_data(db: Session, phone: str, user_id: str, data: dict) -> dict:
    """
    Returns merged session data without saving (for preview usage)
    """
    existing = get_session_data(db, phone, user_id, create=False)
    return {**existing, **data}
