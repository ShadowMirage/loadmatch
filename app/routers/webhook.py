import logging
from fastapi import APIRouter, Request, Response, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.chatbot_service import handle_message
from app.services.whatsapp_service import send_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])

@router.get("")
def verify_webhook(request: Request):
    """WhatsApp verification endpoint"""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        if challenge is not None:
            return int(challenge)
        
    return Response(status_code=403)


@router.post("")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    """Incoming message handler"""
    try:
        body = await request.json()
        
        # WhatsApp Cloud API payload format
        entry = body.get("entry", [])
        if not entry:
            return {"status": "ok"}
            
        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "ok"}
            
        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return {"status": "ok"}
            
        msg = messages[0]
        from_phone = msg.get("from")
        msg_type = msg.get("type")
        
        if not from_phone:
             return {"status": "ok"}

        text = ""
        if msg_type == "text":
            text = msg["text"].get("body", "")
        elif msg_type == "image":
            media_id = msg["image"].get("id")
            caption = msg["image"].get("caption", "")
            text = f"[IMAGE:{media_id}] {caption}".strip()
        else:
            # We don't handle other types currently
            return {"status": "ok"}
            
        # Process the message
        if settings.DEV_MODE:
            reply = f"LoadMatch DEV MODE ✅\nYou said: {text}"
        else:
            reply = await handle_message(from_phone, text, db)

        await send_text(from_phone, reply)
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        
    # Always return 200 OK so WhatsApp doesn't retry endlessly
    return {"status": "ok"}
