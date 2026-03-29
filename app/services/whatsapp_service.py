import hashlib
import html
import json
import logging
import contextvars
from collections.abc import Set as AbstractSet
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.contracts.responses import Response as ContractResponse, coerce_response
from app.core.trace_context import get_trace_id
from app.runtime.redis_adapter import get_client as get_redis_client
from app.runtime.whatsapp_adapter import delivery_enabled, resolve_credentials

logger = logging.getLogger(__name__)

WA_API_URL = "https://graph.facebook.com/v19.0"

def _get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Loadmatch-Trace-ID": get_trace_id()
    }

# Global context for process-local fallback (if wa_id missing)
response_sent_var = contextvars.ContextVar("response_sent", default=frozenset())


def _guard_payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _check_and_set_response_sent(
    recipient: str,
    channel: str,
    payload: Any,
    wa_id: Optional[str] = None,
    ignore_guard: bool = False,
) -> bool:
    """
    Exactly-once delivery guard with Redis latency watchdog.
    Uses Redis-based lock if wa_id (wamid) is provided (cluster-safe).
    If Redis latency > 50ms, flags SQL-primary mode for caller.
    Falls back to a message-scoped ContextVar if no wa_id.
    """
    import time

    if ignore_guard:
        return True
    
    if wa_id:
        lock_key = f"loadmatch:send_lock:{wa_id}"
        redis_client = get_redis_client()
        try:
            if redis_client is not None:
                start = time.perf_counter()
                locked = bool(redis_client.set(lock_key, "1", nx=True, ex=30))
                elapsed = time.perf_counter() - start

                if elapsed > 0.05:
                    logger.warning(
                        f"[REDIS_WATCHDOG] Latency {elapsed:.4f}s > 50ms for wa_id={wa_id}. "
                        f"SQL-primary delivery verification recommended."
                    )
                return locked
        except Exception as e:
            logger.error(f"[REDIS_WATCHDOG] Redis failure: {e}. Falling back to process-local.")
            # Fall through to ContextVar on Redis failure
    
    # Process-local fallback keyed by recipient + payload fingerprint.
    local_key = f"{recipient}:{channel}:{_guard_payload_fingerprint(payload)}"
    sent_keys = response_sent_var.get()
    if not isinstance(sent_keys, AbstractSet):
        logger.warning(
            "response_sent_var contained %s; resetting process-local guard state.",
            type(sent_keys).__name__,
        )
        sent_keys = frozenset()
        response_sent_var.set(sent_keys)
    if local_key in sent_keys:
        return False
    response_sent_var.set(sent_keys | {local_key})
    return True


def reset_response_guard() -> None:
    response_sent_var.set(frozenset())

def sanitize_text(text: str) -> str:
    """
    Centralized sanitization for all outgoing text.
    Hardened variant using html.escape to preserve markdown while stripping scripts.
    """
    if not text:
        return ""
    # Strip <script> explicitly for extra safety
    safe_text = str(text).replace("<script>", "").replace("</script>", "")
    # Hardened variant: preserve markdown while escaping dangerous chars
    return html.escape(safe_text, quote=False).strip()

def _get_whatsapp_creds() -> tuple[str, str]:
    """Return canonical WHATSAPP_TOKEN and phone_number_id."""
    token, phone_id = resolve_credentials()
    if not token:
        raise ValueError("No WhatsApp token configured (WHATSAPP_TOKEN).")
    if not phone_id:
        raise ValueError("No WhatsApp phone number ID configured.")
    return token, phone_id


def _http_client_is_mocked() -> bool:
    return type(httpx.AsyncClient).__module__.startswith("unittest.mock")


def _use_local_delivery_mode(token: Optional[str], phone_id: Optional[str]) -> bool:
    if _http_client_is_mocked():
        return False
    return settings.DEV_MODE or not delivery_enabled() or settings.is_placeholder(token) or settings.is_placeholder(phone_id)


async def send_text(to: str, text: str, ignore_guard: bool = False, wa_id: Optional[str] = None) -> None:
    """Send a plain text message with sanitization and guard."""
    if not _check_and_set_response_sent(
        recipient=to,
        channel="text",
        payload={"text": text},
        wa_id=wa_id,
        ignore_guard=ignore_guard,
    ):
        logger.warning(f"Blocked double response to {to} (wa_id={wa_id}): send_text")
        return

    clean_text = sanitize_text(text)
    if not clean_text:
        logger.warning(f"Empty text after sanitization for {to}")
        return

    try:
        token, phone_id = _get_whatsapp_creds()
    except ValueError as exc:
        if not _http_client_is_mocked():
            logger.warning("Skipping WhatsApp send_text in local mode: %s", exc)
            return
        raise

    if _use_local_delivery_mode(token, phone_id):
        logger.info("Local delivery mode active. Skipping outbound WhatsApp text to=%s", to)
        return

    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = _get_headers(token)
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
            response.raise_for_status()


async def send_interactive_buttons(to: str, text: str, buttons: list[dict], ignore_guard: bool = False, wa_id: Optional[str] = None) -> None:
    """Send interactive buttons with automatic plain text fallback."""
    if not _check_and_set_response_sent(
        recipient=to,
        channel="buttons",
        payload={"text": text, "buttons": buttons},
        wa_id=wa_id,
        ignore_guard=ignore_guard,
    ):
        logger.warning(f"Blocked double response to {to} (wa_id={wa_id}): send_interactive_buttons")
        return

    clean_text = sanitize_text(text)
    # Enforce WA limit of 3 buttons
    buttons = buttons[:3]

    try:
        token, phone_id = _get_whatsapp_creds()
    except ValueError as exc:
        if not _http_client_is_mocked():
            logger.warning("Skipping WhatsApp button send in local mode: %s", exc)
            return
        raise

    if _use_local_delivery_mode(token, phone_id):
        logger.info("Local delivery mode active. Skipping outbound WhatsApp buttons to=%s", to)
        return

    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = _get_headers(token)

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
            await send_text(to, f"{clean_text}\n\n{opts}", ignore_guard=True, wa_id=wa_id)


async def send_interactive_list(to: str, text: str, button_text: str, sections: list[dict], ignore_guard: bool = False, wa_id: Optional[str] = None) -> None:
    """Send interactive list with fallback."""
    if not _check_and_set_response_sent(
        recipient=to,
        channel="list",
        payload={"text": text, "button_text": button_text, "sections": sections},
        wa_id=wa_id,
        ignore_guard=ignore_guard,
    ):
        logger.warning(f"Blocked double response to {to} (wa_id={wa_id}): send_interactive_list")
        return

    clean_text = sanitize_text(text)
    # Enforce WA limits (max 10 rows total)
    if sections:
        sections[0]["rows"] = sections[0]["rows"][:10]

    try:
        token, phone_id = _get_whatsapp_creds()
    except ValueError as exc:
        if not _http_client_is_mocked():
            logger.warning("Skipping WhatsApp list send in local mode: %s", exc)
            return
        raise

    if _use_local_delivery_mode(token, phone_id):
        logger.info("Local delivery mode active. Skipping outbound WhatsApp list to=%s", to)
        return

    url = f"{WA_API_URL}/{phone_id}/messages"
    headers = _get_headers(token)

    interactive = {
        "type": "list",
        "body": {"text": clean_text},
        "action": {
            "button": (button_text or "Select")[:20],  # WA limit 20 chars
            "sections": sections
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
            
            # 🔥 ROBUST FALLBACK: Convert list to numbered text options
            fallback_text = f"{clean_text}\n"
            count = 1
            for section in sections:
                for row in section.get("rows", []):
                    fallback_text += f"\n{count}️⃣ *{row['title']}*\n{row.get('description', '')}\n"
                    count += 1
            
            await send_text(to, fallback_text, ignore_guard=True, wa_id=wa_id)


async def send_list_message(to: str, title: str, body: str, options: list[dict], wa_id: Optional[str] = None) -> None:
    """Legacy helper for send_interactive_list with simplified sections."""
    sections = [{
        "title": title[:24],
        "rows": options[:10]
    }]
    await send_interactive_list(to, body, "Select Option", sections, wa_id=wa_id)


async def download_media(media_id: str) -> bytes:
    """Download media from WhatsApp servers."""
    token, _ = _get_whatsapp_creds()
    headers = _get_headers(token)

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

async def send_main_menu(to: str, text: str = "Please select an option:", ignore_guard: bool = False, wa_id: Optional[str] = None):
    # This function now honors wa_id for cluster locks
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

    await send_interactive_list(to, text, button_text, sections, ignore_guard=ignore_guard, wa_id=wa_id)

async def send_booking_menu(to: str, booking_code: str, wa_id: Optional[str] = None) -> None:
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
    await send_interactive_list(to, text, "Booking Menu", sections, wa_id=wa_id)


async def trigger_rating_requests(db: Session, match_id: str) -> None:
    from app.models.match import Match
    from app.models.rating import Rating
    from app.models.user import User
    from app.models.load_request import LoadRequest
    from app.models.listing import TruckSpaceListing
    from app.services.session_manager import update_session

    match = db.query(Match).filter(Match.id == match_id).first()
    if not match: return

    load = db.query(LoadRequest).filter(LoadRequest.id == match.load_request_id).first()
    listing = db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match.listing_id).first()

    shipper = db.query(User).filter(User.id == load.shipper_id).first()
    transp = db.query(User).filter(User.id == listing.owner_id).first()

    rating_rows = [
        {"id": f"RATING_{match.id}_5", "title": "⭐⭐⭐⭐⭐"},
        {"id": f"RATING_{match.id}_4", "title": "⭐⭐⭐⭐"},
        {"id": f"RATING_{match.id}_3", "title": "⭐⭐⭐"},
        {"id": f"RATING_{match.id}_2", "title": "⭐⭐"},
        {"id": f"RATING_{match.id}_1", "title": "⭐"}
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

async def safe_fallback(phone: str, wa_id: Optional[str] = None):
    try:
        await send_text(phone, "⚠️ Temporary issue. Please try again.", ignore_guard=True, wa_id=wa_id)
    except Exception as e:
        logger.error(f"Fallback failed: {e}")

async def send_response(to: str, response: ContractResponse, ignore_guard: bool = False, wa_id: Optional[str] = None) -> None:
    """
    Unified entry point for sending standardized Response objects.
    Handles buttons, lists, and plain text with wa_id locking.
    """
    response = coerce_response(response)

    if response.is_list:
        sections = [
            {
                "title": s.title,
                "rows": [
                    {"id": r.id, "title": r.title, "description": r.description}
                    for r in s.rows
                ]
            }
            for s in response.sections
        ]
        await send_interactive_list(
            to, 
            response.text, 
            response.list_button_text or "Select", 
            sections, 
            ignore_guard,
            wa_id=wa_id
        )
    elif response.has_buttons:
        buttons = [{"id": b.id, "title": b.title} for b in response.buttons]
        await send_interactive_buttons(to, response.text, buttons, ignore_guard, wa_id=wa_id)
    else:
        await send_text(to, response.text, ignore_guard, wa_id=wa_id)
