import logging
from fastapi import APIRouter, Request, Response, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.services.chatbot_service import handle_message
from app.services.whatsapp_service import send_text, send_main_menu, response_sent_var
from app.services.session_manager import get_session, get_session_data
from app.services.workflow_router import route_interactive_payload, handle_new_user_onboarding, dispatch_ai_action, handle_text_command
from app.services.event_logger import track_event
from app.services.kyc_service import handle_kyc_image
from app.services.deduplication_service import is_duplicate, mark_processed
from app.services.rate_limiter import is_rate_limited
from app.services.abuse_prevention import detect_spam

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])

async def safe_fallback(phone: str):
    try:
        await send_text(
            phone,
            "⚠️ Temporary issue. Please try again.",
            ignore_guard=True
        )
    except Exception as e:
        logger.error(f"Critical failure in safe_fallback: {e}")

# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------

@router.get("")
def verify_webhook(
    hub_mode:str = Query(None, alias="hub.mode"),
    hub_verify_token:str = Query(None, alias="hub.verify_token"),
    hub_challenge:str = Query(None, alias="hub.challenge"),
):
    verify_token = settings.WHATSAPP_VERIFY_TOKEN or settings.whatsapp_verify_token
    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

# ---------------------------------------------------------------------------
# Incoming message handler (Mandated 9-Step Flow)
# ---------------------------------------------------------------------------

@router.post("")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Surgical Webhook Pipeline:
    1. Deduplication | 2. Rate limit | 3. User | 4. Normalize | 5. SHORT-CIRCUIT
    6. Commands | 7. AI Engine | 8. Dispatch | 9. Final Response Egress
    """
    # Initialize response guard
    response_sent_var.set(False)

    try:
        body = await request.json()
        messages = body.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [])
        if not messages:
            return {"status": "ok"}

        msg = messages[0]
        phone = msg.get("from")
        msg_type = msg.get("type")
        wa_id = msg.get("id")

        if not phone: return {"status": "ok"}

        # 1. Deduplication
        if wa_id and (is_duplicate(db, wa_id) or not mark_processed(db, wa_id)):
            return {"status": "ok"}

        # 2. Rate Limit & Anti-Spam
        user = db.query(User).filter(User.phone == phone).first()
        if user and (detect_spam(user.id, wa_id) or is_rate_limited(db, user.id)):
            await send_text(phone, "⏳ Too many messages. Please wait.")
            return {"status": "ok"}

        # 3. User Fetch/Create
        if not user:
            user = User(phone=phone)
            db.add(user)
            db.commit()
            db.refresh(user)

        # 4. Input Normalization
        raw_text = msg.get("text", {}).get("body", "").strip() if msg_type == "text" else ""
        norm_text = raw_text.lower() if raw_text else ""
        
        # 5. SHORT-CIRCUIT (Mandated location)
        session = get_session(db, phone, user.id)

        if quick and quick.get("from") and quick.get("to"):
            session_data = session.session_data if isinstance(session.session_data, dict) else {}
            safe_quick = quick if isinstance(quick, dict) else {}
            session_data = session.session_data if isinstance(session.session_data, dict) else {}
            safe_quick = quick if isinstance(quick, dict) else {}

            data = {**session_data, **safe_quick}
            act = "confirm_truck_listing" if session.current_workflow == "TRUCK_FLOW" else "confirm_load_request"
            await dispatch_ai_action(phone, {"action": act, "data": data}, user, db)
            return {"status": "ok"}

        # 6. Command Interception (Bypass AI)
        if msg_type == "interactive":
            payload_id = msg.get("interactive", {}).get("button_reply", {}).get("id") or \
                         msg.get("interactive", {}).get("list_reply", {}).get("id")
            if payload_id:
                await route_interactive_payload(phone, payload_id, db, user)
                return {"status": "ok"}

        if msg_type == "text":
            if norm_text in ["hi", "hello", "menu", "status"]:
                await send_main_menu(phone)
                return {"status": "ok"}
            if await handle_text_command(phone, user, raw_text, db):
                return {"status": "ok"}

        # 7. AI / Deterministic Processing
        if msg_type == "image":
            await handle_kyc_image(user.id, phone, msg["image"]["id"], db)
            return {"status": "ok"}

        if msg_type == "text":
            # Onboarding check
            if not user.wa_onboarded:
                if await handle_new_user_onboarding(phone, user, raw_text, "text", db):
                    return {"status": "ok"}

            # Standard AI Pipeline
            reply, action = await handle_message(phone, raw_text, db)
            print("DEBUG AI ACTION:", action, type(action))

            # 🔒 ACTION SAFETY
           # 🔒 FINAL SAFETY CHECK BEFORE DISPATCH
            if not isinstance(action, dict):
                print("⚠️ ACTION NOT DICT IN WEBHOOK:", action, type(action))
                action = None

            if action and "data" in action and not isinstance(action["data"], dict):
                print("⚠️ ACTION DATA NOT DICT IN WEBHOOK:", action["data"], type(action["data"]))
                action["data"] = {}
            # 8. Action Dispatch
            if action:
                print("FINAL ACTION:", action, type(action))
                await dispatch_ai_action(phone, action, user, db)
            elif reply:
                await send_text(phone, reply)
            elif action:
                await dispatch_ai_action(phone, action, user, db)
            else:
                await send_main_menu(phone)

    except Exception as e:
        logger.error(f"Surgical Webhook Pipeline Failure: {e}")
        await safe_fallback(phone)

    return {"status": "ok"}
