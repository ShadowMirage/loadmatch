import json
import logging
import re
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.conversation import Conversation
from app.services.session_manager import get_session, get_session_data
from app.services.ai_extraction_service import extract_with_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quick_extract(text: str) -> dict:
    import re

    t = text.lower()
    data = {}

    # ✅ PRICE FIX (moved after data init)
    price_match = re.search(r'(\d+(?:\.\d+)?)\s*/?\s*kg', t)
    if price_match:
        data["rate_per_kg"] = float(price_match.group(1))

    route = re.search(
        r"(?:from\s+(\w+)\s+to\s+(\w+)|(\w+)\s+se\s+(\w+)|(\w+)\s+to\s+(\w+))",
        t
    )
    weight = re.search(r"(\d+(?:\.\d+)?)\s*(ton|tons|kg|tonne)", t)
    plate = re.search(r"[a-z]{2}\d{2}[a-z]{1,3}\d{4}", t)
    date = re.search(r"(tomorrow|today|kal|aaj)", t)

    if route:
        data["from"] = (route.group(1) or route.group(3) or route.group(5) or "").title()
        data["to"] = (route.group(2) or route.group(4) or route.group(6) or "").title()

    if weight:
        val = float(weight.group(1))
        if "ton" in weight.group(2):
            val *= 1000
        data["weight_kg"] = int(val)

    if plate:
        data["plate"] = plate.group(0).upper()

    if date:
        d = date.group(1)
        data["date"] = "tomorrow" if d in ["kal", "tomorrow"] else "today"

    return data


def _infer_fallback_from_text(text: str) -> tuple[str, dict | None]:
    """
    When AI fails, attempt to extract partial entities from raw text.
    Returns (reply_text, action_or_None).
    """
    if not text:
        return ("❓ Could you clarify your request?", None)

    t = text.lower()
    route_match = re.search(r"(?:from\s+(\w+)\s+to\s+(\w+)|(\w+)\s+se\s+(\w+))", t)
    weight_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:ton(?:ne)?s?|kg|tonne)", t)
    
    if route_match and weight_match:
        frm = (route_match.group(1) or route_match.group(3) or "?").title()
        to  = (route_match.group(2) or route_match.group(4) or "?").title()
        kg  = float(weight_match.group(1))
        return (None, {"action": "ask_missing_field", "field": "date", "data": {"from": frm, "to": to, "weight_kg": kg}})

    if route_match:
        frm = (route_match.group(1) or route_match.group(3) or "?").title()
        to  = (route_match.group(2) or route_match.group(4) or "?").title()
        return (None, {"action": "ask_missing_field", "field": "weight_kg", "data": {"from": frm, "to": to}})

    return ("📦 Tell me what you're looking for (e.g., '10 ton Jaipur to Delhi tomorrow').", None)


def render_truck_confirmation(data: dict) -> tuple[str, list[dict]]:
    body = (
        f"🚚 Confirm Truck Listing\n\n"
        f"📍 {data.get('from','?').title()} → {data.get('to','?').title()}\n"
        f"⚖️ {data.get('weight_kg', data.get('capacity_kg', '?'))} kg\n"
        f"💰 ₹{data.get('rate_per_kg','?')} / kg\n"
        f"📅 {data.get('date','?')}"
    )
    buttons = [
        {"id": "CONFIRM_TRUCK_LISTING", "title": "✅ Confirm"},
        {"id": "EDIT_TRUCK", "title": "✏️ Edit"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"}
    ]
    return body, buttons

def render_truck_confirmation(data: dict) -> tuple[str, list[dict]]:
    body = (
        f"🚚 Confirm Truck Listing\n\n"
        f"📍 {data.get('from','?').title()} → {data.get('to','?').title()}\n"
        f"⚖️ {data.get('capacity_kg', data.get('weight_kg', '?'))} kg\n"
        f"💰 ₹{data.get('rate_per_kg','?')} / kg\n"
        f"📅 {data.get('date','?')}"
    )

    buttons = [
        {"id": "CONFIRM_TRUCK_LISTING", "title": "✅ Confirm"},
        {"id": "EDIT_TRUCK", "title": "✏️ Edit"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"}
    ]

    return body, buttons


# ---------------------------------------------------------------------------
# Public interface (STRICT CONTRACT: returns (reply, action))
# ---------------------------------------------------------------------------

async def handle_message(phone: str, text: str, db: Session) -> tuple[str | None, dict | None]:

    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        return ("User not found.", None)

    # 1. Deterministic extraction
    quick = _quick_extract(text)

    # 2. Session merge (SAFE)
    existing_data = get_session_data(db, phone, user.id)
    if not isinstance(existing_data, dict):
        existing_data = {}

    merged = {**existing_data, **quick}

    # Save conversation
    db.add(Conversation(user_id=user.id, session_id=phone, role="user", content=text))
    db.commit()

    try:
        # 3. AI extraction
        action = await extract_with_context(text, merged)

# 🔥 FORCE PARSE IF STRING (CRITICAL FIX)
        if isinstance(action, str):

            cleaned = action.strip()

    # 🔥 FIX 1: wrap broken fragments
            if not cleaned.startswith("{"):
                cleaned = "{" + cleaned
            if not cleaned.endswith("}"):
                cleaned = cleaned + "}"

    # 🔥 FIX 2: remove leading junk (like \n or text)
            cleaned = re.sub(r"^[^{]*", "", cleaned)

            try:
                action = json.loads(cleaned)
            except Exception as e:
                logger.error(f"AI JSON parse failed: {cleaned} | Error: {e}")
                action = None

# Final validation
        if not isinstance(action, dict):
            action = None
        # FINAL SAFETY NET
        if action and "action" not in action:
            logger.error(f"Malformed AI response: {action}")
            action = None

        # 4. SESSION-AWARE FALLBACK (🔥 CRITICAL FIX)
        if not action:

            session = get_session(db, phone, user.id)
            current_wf = session.current_workflow if session else None

            if current_wf in ["TRUCK_FLOW", "CONFIRMATION_PENDING"]:
                return (None, {
                    "action": "confirm_truck_listing",
                    "data": merged
                })

            if current_wf in ["LOAD_FLOW", "CONFIRMATION_PENDING"]:
                return (None, {
                    "action": "confirm_load_request",
                    "data": merged
                })

            # fallback NLP
            reply, fallback_action = _infer_fallback_from_text(text)

            if fallback_action:
                return (None, fallback_action)

            return ("👋 Hello! How can I help?", {"action": "general_chat"})

        return (None, action)

    except Exception as e:
        logger.error(f"Intelligence failure: {e}")

        # SAFE FALLBACK
        reply, fallback_action = _infer_fallback_from_text(text)

        if fallback_action:
            return (None, fallback_action)

        return ("⚠️ Something went wrong. Please try again.", None)