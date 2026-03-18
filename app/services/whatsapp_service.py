import logging
import httpx
import contextvars
import re
from sqlalchemy.orm import Session
from app.config import settings

logger = logging.getLogger(__name__)

WA_API_URL = "https://graph.facebook.com/v19.0"

# Global context for double-send protection
response_sent_var = contextvars.ContextVar("response_sent", default=False)

def _check_and_set_response_sent(ignore_guard: bool = False) -> bool:
    """Internal guard to prevent multiple responses for a single request."""
    if ignore_guard:
        return True
    if response_sent_var.get():
        return False
    response_sent_var.set(True)
    return True

def sanitize_text(text: str) -> str:
    """
    Centralized sanitization for all outgoing text.
    - Strips JSON-like curly brace blocks
    - Strips URLs
    - Strips markdown code blocks and backticks
    """
    if not text:
        return ""
    # Strip greedy JSON blocks
    text = re.sub(r"\{.*?\}", "", text, flags=re.DOTALL)
    # Strip remaining unclosed braces
    text = re.sub(r"\{.*", "", text, flags=re.DOTALL)
    # Strip URLs
    text = re.sub(r"http[s]?://\S+", "", text)
    # Strip markdown code blocks
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`.*?`", "", text)
    return text.strip()

def _get_whatsapp_creds() -> tuple[str, str]:
    """Return (token, phone_number_id) from settings."""
    token = (
        settings.WHATSAPP_TOKEN
        or settings.WA_TOKEN
        or settings.WHATSAPP_ACCESS_TOKEN
    )
    phone_id = (
        settings.WHATSAPP_PHONE_NUMBER_ID
        or settings.WA_PHONE_NUMBER_ID
    )
    if not token:
        raise ValueError("No WhatsApp token configured.")
    if not phone_id:
        raise ValueError("No WhatsApp phone number ID configured.")
    return token, phone_id


async def send_text(to: str, text: str, ignore_guard: bool = False) -> None:
    """Send a plain text message with sanitization and guard."""
    if not _check_and_set_response_sent(ignore_guard):
        logger.warning(f"Blocked double response to {to}: send_text")
        return

    clean_text = sanitize_text(text)
    if not clean_text:
        logger.warning(f"Empty text after sanitization for {to}")
        return

    token, phone_id = _get_whatsapp_creds()
    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": clean_text}
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(f"WhatsApp API Error (send_text) to={to} status={response.status_code} body={response.text}")


async def send_interactive_buttons(to: str, text: str, buttons: list[dict], ignore_guard: bool = False) -> None:
    """Send interactive buttons with automatic plain text fallback."""
    if not _check_and_set_response_sent(ignore_guard):
        logger.warning(f"Blocked double response to {to}: send_interactive_buttons")
        return

    clean_text = sanitize_text(text)
    # Enforce WA limit of 3 buttons
    buttons = buttons[:3]

    token, phone_id = _get_whatsapp_creds()
    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    interactive = {
        "type": "button",
        "body": {"text": clean_text},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": btn["id"], "title": btn["title"]}}
                for btn in buttons
            ]
        }
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(f"WhatsApp API Error (buttons) to={to} status={response.status_code} falling back to text")
            # Fallback to plain text if interactive fails (ignore guard since we already set it)
            opts = "\n".join([f"• {b['title']}" for b in buttons])
            await send_text(to, f"{clean_text}\n\n{opts}", ignore_guard=True)


async def send_interactive_list(to: str, text: str, button_text: str, sections: list[dict], ignore_guard: bool = False) -> None:
    """Send interactive list with fallback."""
    if not _check_and_set_response_sent(ignore_guard):
        logger.warning(f"Blocked double response to {to}: send_interactive_list")
        return

    clean_text = sanitize_text(text)
    token, phone_id = _get_whatsapp_creds()
    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    interactive = {
        "type": "list",
        "body": {"text": clean_text},
        "action": {
            "button": button_text,
            "sections": [
                {
                    "title": section["title"],
                    "rows": [
                        {"id": row["id"], "title": row["title"]}
                        for row in section["rows"]
                    ]
                }
                for section in sections
            ]
        }
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(f"WhatsApp API Error (list) to={to} status={response.status_code} falling back to text")
            await send_text(to, clean_text, ignore_guard=True)


async def download_media(media_id: str) -> bytes:
    """Download media from WhatsApp servers."""
    token, _ = _get_whatsapp_creds()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        res = await client.get(f"{WA_API_URL}/{media_id}", headers=headers)
        res.raise_for_status()
        media_url = res.json().get("url")
        if not media_url:
            raise ValueError(f"No URL for media_id: {media_id}")
        
        content_res = await client.get(media_url, headers=headers)
        content_res.raise_for_status()
        return content_res.content


# Helper Menus

async def send_main_menu(to: str, text: str = "Please select an option:", ignore_guard: bool = False):

    # 🔥 HARD LOCK
    if response_sent_var.get():
        logger.warning("Main menu blocked (already sent)")
        return

    response_sent_var.set(True)

    button_text = "Main Menu"
    sections = [
        {
            "title": "Options",
            "rows": [
                {"id": "POST_TRUCK", "title": "🚚 Post Truck"},
                {"id": "FIND_TRUCK", "title": "🔍 Find Truck"},
                {"id": "TRACK_BOOKING", "title": "📊 Track Booking"},
                {"id": "DELIVERY_STATUS", "title": "📦 Delivery Status"},
                {"id": "UPLOAD_KYC", "title": "📄 Upload KYC"},
            ]
        }
    ]

    await send_interactive_list(to, text, button_text, sections, ignore_guard=True)

async def send_booking_menu(to: str, booking_code: str) -> None:
    text = f"Manage Booking: {booking_code}"
    sections = [{
        "title": "Actions",
        "rows": [
            {"id": f"CONFIRM_BOOKING_{booking_code}", "title": "Confirm Booking"},
            {"id": f"TRACK_TRUCK_{booking_code}", "title": "Track Truck"},
            {"id": f"CONTACT_DRIVER_{booking_code}", "title": "Contact Driver"},
            {"id": f"CANCEL_BOOKING_{booking_code}", "title": "Cancel Booking"}
        ]
    }]
    await send_interactive_list(to, text, "Booking Menu", sections)


async def trigger_rating_requests(db: Session, match_id: str) -> None:
    from app.models.match import Match
    from app.models.rating import Rating
    from app.models.user import User
    from app.models.load_request import LoadRequest
    from app.models.listing import TruckSpaceListing
    from app.services.session_manager import update_session, get_session

    match = db.query(Match).filter(Match.id == match_id).first()
    if not match: return

    load = db.query(LoadRequest).filter(LoadRequest.id == match.load_request_id).first()
    listing = db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match.listing_id).first()

    shipper = db.query(User).filter(User.id == load.shipper_id).first()
    transp = db.query(User).filter(User.id == listing.owner_id).first()

    rating_rows = [
        {"id": "RATING_5", "title": "⭐⭐⭐⭐⭐"},
        {"id": "RATING_4", "title": "⭐⭐⭐⭐"},
        {"id": "RATING_3", "title": "⭐⭐⭐"},
        {"id": "RATING_2", "title": "⭐⭐"},
        {"id": "RATING_1", "title": "⭐"}
    ]
    sections = [{"title": "Rate Experience", "rows": rating_rows}]

    # Shipper rates transporter
    if not db.query(Rating).filter(Rating.match_id == match.id, Rating.rater_id == shipper.id).first():
        update_session(db, shipper.phone, {"current_workflow": "rate_trip"})
        await send_interactive_list(shipper.phone, "Trip Completed ✅\nRate the transporter.", "Rate Now", sections)

    # Transporter rates shipper
    if not db.query(Rating).filter(Rating.match_id == match.id, Rating.rater_id == transp.id).first():
        update_session(db, transp.phone, {"current_workflow": "rate_trip"})
        await send_interactive_list(transp.phone, "Trip Completed ✅\nRate the shipper.", "Rate Now", sections)

async def safe_fallback(phone: str):
    try:
        await send_text(phone, "⚠️ Temporary issue. Please try again.", ignore_guard=True)
    except Exception as e:
        print("Fallback failed:", e)

async def send_list_message(phone: str, header: str, body: str, options: list[dict], ignore_guard: bool = False):
    """
    Send WhatsApp list message (supports up to 10 items)
    """

    if not _check_and_set_response_sent(ignore_guard):
        logger.warning(f"Blocked double response to {phone}: send_list_message")
        return

    clean_body = sanitize_text(body)

    token, phone_id = _get_whatsapp_creds()
    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    sections = [{
        "title": "Available Trucks",
        "rows": [
            {
                "id": opt["id"],
                "title": opt["title"],
                "description": opt.get("description", "")
            }
            for opt in options[:10]
        ]
    }]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header},
            "body": {"text": clean_body},
            "footer": {"text": "Select a truck"},
            "action": {
                "button": "View Trucks",
                "sections": sections
            }
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(f"WhatsApp API Error (list) {response.text}")
            await send_text(phone, clean_body, ignore_guard=True)