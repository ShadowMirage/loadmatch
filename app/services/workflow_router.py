import logging
import asyncio
from datetime import datetime, timedelta, date
from sqlalchemy.orm import Session

from app.services.session_manager import (
    get_session, get_session_data, update_session, clear_session, set_session_data
)
from app.services.whatsapp_service import (
    send_text, send_main_menu, send_interactive_buttons
)
from app.services.city_normalizer import normalize_city
from app.models.user import User
from app.models.enums import UserRole
from app.models.route_subscription import RouteSubscription
from app.models.listing import TruckSpaceListing, ListingStatus
from app.models.load_request import LoadRequest, LoadRequestStatus
from app.models.truck import Truck
from app.models.enums import TruckType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date_safe(val: str) -> date:
    """Robust date parser for all user inputs."""
    if not val:
        return datetime.utcnow().date()

    val = str(val).strip().lower()

    # Natural language support
    if val == "today":
        return datetime.utcnow().date()

    if val == "tomorrow":
        return (datetime.utcnow() + timedelta(days=1)).date()

    # Try standard format
    try:
        return datetime.strptime(val, "%d-%m-%Y").date()
    except:
        pass

    # Try ISO format fallback
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except:
        pass

    return datetime.utcnow().date()

def validate_action(action: str, current_workflow: str) -> bool:
    """Enforces valid state machine transitions."""

    allowed_map = {
        "LOAD_FLOW": ["confirm_load_request", "ask_missing_field"],
        "TRUCK_FLOW": ["confirm_truck_listing", "ask_missing_field"],
        
        # ✅ FIXED: allow confirmation actions
        "CONFIRMATION_PENDING": [
            "confirm_load_request",
            "confirm_truck_listing",
            "ask_missing_field"
        ],

        "MAIN_MENU": [
            "confirm_load_request",
            "confirm_truck_listing",
            "ask_missing_field",
            "general_chat"
        ]
    }

    wf = current_workflow or "MAIN_MENU"
    allowed = allowed_map.get(wf, ["ask_missing_field", "general_chat"])

    return action in allowed or action == "general_chat"

def generate_booking_id(phone: str, db: Session) -> str:
    count = db.query(LoadRequest).filter(
        LoadRequest.shipper_id != None
    ).count() + 1

    return f"LM{phone[-4:]}{str(count).zfill(4)}"


# ---------------------------------------------------------------------------
# Route Subscriptions
# ---------------------------------------------------------------------------

async def subscribe_route(phone: str, user: User, pickup: str, drop: str, db: Session) -> None:
    pk = normalize_city(pickup).lower()
    dp = normalize_city(drop).lower()
    
    if not db.query(RouteSubscription).filter_by(user_id=user.id, normalized_pickup=pk, normalized_drop=dp).first():
        db.add(RouteSubscription(user_id=user.id, pickup_city=pickup.title(), drop_city=drop.title(),
                                 normalized_pickup=pk, normalized_drop=dp))
        db.commit()
    
    await send_text(phone, f"🔔 Alerts enabled for {pickup.title()} → {drop.title()}.")

async def unsubscribe_route(phone: str, user: User, pickup: str, drop: str, db: Session) -> None:
    pk = normalize_city(pickup).lower()
    dp = normalize_city(drop).lower()
    
    sub = db.query(RouteSubscription).filter_by(user_id=user.id, normalized_pickup=pk, normalized_drop=dp).first()
    if sub:
        db.delete(sub)
        db.commit()
    
    await send_text(phone, f"🔕 Alerts disabled for {pickup.title()} → {drop.title()}.")


# ---------------------------------------------------------------------------
# Execution Layer (Single Commit Point)
# ---------------------------------------------------------------------------

async def dispatch_ai_action(phone: str, action: dict, user: User, db: Session) -> None:
    """
    Main execution dispatcher.
    ONLY this function and route_interactive_payload commit to DB.
    """
    act = action.get("action")
    data = action.get("data", {})

    user_session = get_session(db, phone, user.id)
    current_wf = user_session.current_workflow or "MAIN_MENU"

    # Merge intelligence proposal into session
    existing = get_session_data(db, phone, user.id) or {}
    merged = {**existing, **data}

    # 🔥 AUTO-CORRECT ACTION BASED ON WORKFLOW
    
    if not validate_action(act, current_wf):

        logger.warning(f"Invalid action {act} for workflow {current_wf}")

        if current_wf == "TRUCK_FLOW":
            act = "confirm_truck_listing"
            action["action"] = act

        elif current_wf == "LOAD_FLOW":
            act = "confirm_load_request"
            action["action"] = act

        else:
            return
    if current_wf == "CONFIRMATION_PENDING":
        logger.info("Already in confirmation state — skipping duplicate")
        return

    # Commit state changes
    if act == "confirm_load_request":
        set_session_data(db, phone, user.id, merged)
        update_session(db, phone, {"current_workflow": "CONFIRMATION_PENDING"})
        from app.services.chatbot_service import render_load_confirmation
        body, buttons = render_load_confirmation(merged)
        await send_interactive_buttons(phone, body, buttons)

    elif act == "confirm_truck_listing":

        REQUIRED_FIELDS = ["plate", "from", "to", "date", "capacity_kg", "rate_per_kg"]

        def is_missing(val):
            return val is None or val == ""

        # 🔥 Normalize capacity
        if not merged.get("capacity_kg") and merged.get("weight_kg"):
            merged["capacity_kg"] = merged["weight_kg"]

        missing = [f for f in REQUIRED_FIELDS if is_missing(merged.get(f))]

        if missing:
            set_session_data(db, phone, user.id, merged)

            PRIORITY = ["plate", "from", "to", "date", "capacity_kg", "rate_per_kg"]

            FIELD_PROMPTS = {
                "plate": "🚛 What is your truck number?",
                "from": "📍 From which city?",
                "to": "🏁 To which city?",
                "date": "📅 What date?",
                "capacity_kg": "⚖️ What is truck capacity (kg)?",
                "rate_per_kg": "💰 What is your price per kg?"
            }

            next_field = next((f for f in PRIORITY if f in missing), missing[0])
            prompt = FIELD_PROMPTS.get(next_field, f"Please provide {next_field}")

            print(f"[TRUCK FLOW MISSING]: {missing}", flush=True)

            await send_text(phone, prompt)
            return

        # ✅ ONLY ONE CONFIRMATION (FIXED)
        set_session_data(db, phone, user.id, merged)
        update_session(db, phone, {"current_workflow": "CONFIRMATION_PENDING"})

        from app.services.chatbot_service import render_truck_confirmation
        body, buttons = render_truck_confirmation(merged)

        await send_interactive_buttons(
            phone,
            body,
            buttons,
            ignore_guard=True
        )

    elif act == "ask_missing_field":
        set_session_data(db, phone, user.id, merged)
        prompts = {
            "from": "📍 From which city?",
            "to": "🏁 To which city?",
            "date": "📅 What date?",
            "weight_kg": "⚖️ What is the weight (kg)?",
            "capacity_kg": "⚖️ What is truck capacity (kg)?",
            "rate_per_kg": "💰 What is price per kg?",
            "plate": "🚛 Truck plate number?"
        }
        p = prompts.get(action.get("field"), "Please provide more details.")
        await send_text(phone, p)

    elif act == "general_chat":
        await send_main_menu(phone)


async def route_interactive_payload(phone: str, payload: str, db: Session, user: User):
    """Handles button/list replies."""
    p_id = payload.strip().upper()
    user_session = get_session(db, phone, user.id)

    if p_id == "MAIN_MENU":
        clear_session(db, phone)
        await send_main_menu(phone)

    elif p_id == "FIND_TRUCK":
        update_session(db, phone, {"current_workflow": "LOAD_FLOW"})
        await send_text(phone, "📦 What are you shipping? (e.g. '10 ton Jaipur to Delhi tomorrow')")

    elif p_id == "POST_TRUCK":
        update_session(db, phone, {"current_workflow": "TRUCK_FLOW"})
        await send_text(phone, "🚚 Tell me about your truck. (e.g. 'RJ14AB1234 7 ton Jaipur to Agra')")
    
    

    elif p_id == "CONFIRM_LOAD_REQUEST":

        data = get_session_data(db, phone, user.id) or {}

        REQUIRED_FIELDS = ["from", "to", "date", "weight_kg"]

        def is_missing(val):
            return val is None or val == "" or val == "?"

        missing = [f for f in REQUIRED_FIELDS if is_missing(data.get(f))]

        if missing:
            FIELD_PROMPTS = {
                "from": "📍 From which city?",
                "to": "🏁 To which city?",
                "date": "📅 What date?",
                "weight_kg": "⚖️ What is the weight (kg)?"
            }

        next_field = missing[0]

        await send_text(phone, FIELD_PROMPTS.get(next_field))
        return
        

        # ✅ CREATE LOAD
        booking_id = generate_booking_id(phone, db)

        existing = get_session_data(db, phone, user.id) or {}

        set_session_data(db, phone, user.id, {
            **existing,
            **data,
            "booking_id": booking_id
        })

        load = LoadRequest(
            shipper_id=user.id,
            from_city=data.get("from", "").title(),
            to_city=data.get("to", "").title(),
            pickup_date=parse_date_safe(data.get("date", "")),
            weight_kg=weight_kg,
            goods_type=data.get("cargo", "general goods"),
            status=LoadRequestStatus.open,
            booking_id=booking_id
        )

        db.add(load)
        db.commit()

        # ✅ FETCH MATCHING TRUCKS
        trucks = db.query(TruckSpaceListing).filter(
            TruckSpaceListing.from_city == load.from_city,
            TruckSpaceListing.to_city == load.to_city,
            TruckSpaceListing.status == ListingStatus.open
        ).limit(10).all()

        # ✅ FORMAT FOR LIST MESSAGE
        truck_options = []
        for t in trucks:
            truck_options.append({
                "id": f"SELECT_TRUCK_{t.id}",
                "title": f"{t.available_capacity_kg}kg | ₹{t.price_per_kg}/kg",
                "description": f"{t.from_city} → {t.to_city} | {t.departure_date}"
            })

        clear_session(db, phone)

        # ✅ SEND LIST (NOT BUTTONS)
        if truck_options:
            from app.services.whatsapp_service import send_list_message

            await send_list_message(
                phone,
                "🚚 Available Trucks",
                f"📦 Load Posted!\n🆔 Booking ID: {booking_id}\n\nSelect a truck:",
                truck_options
            )
        else:
            clear_session(db, phone)

            await send_interactive_buttons(
                phone,
                f"📦 Load Posted!\n🆔 Booking ID: {booking_id}\n\nNo trucks available right now.",
                [{"id": "MAIN_MENU", "title": "Main Menu"}]
            )
    elif p_id == "CONFIRM_TRUCK_LISTING":
        data = get_session_data(db, phone, user.id) or {}
        
        # Ensure truck exists (Auto-create if plate provided)
        truck_id = user.trucks[0].id if user.trucks else None
        if not truck_id and data.get("plate"):
            new_truck = Truck(owner_id=user.id, registration_number=data["plate"].upper(),
                              truck_type=TruckType.large, total_capacity_kg=int(data.get("weight_kg") or 0))
            db.add(new_truck)
            db.commit()
            truck_id = new_truck.id

        if truck_id:
            rate = data.get("rate_per_kg")
            if rate is None:
                rate = 1.0  # fallback safety

            listing = TruckSpaceListing(
                owner_id=user.id,
                truck_id=truck_id,
                from_city=data.get("from", "").title(),
                to_city=data.get("to", "").title(),
                departure_date=parse_date_safe(data.get("date", "")),
                total_capacity_kg=int(data.get("weight_kg") or 0),
                available_capacity_kg=int(data.get("weight_kg") or 0),
                price_per_kg=float(data["rate_per_kg"]),
                status=ListingStatus.open
            )
            db.add(listing)
            db.commit()
            clear_session(db, phone)
            listing_id = f"TL{str(listing.id).zfill(5)}"

            await send_interactive_buttons(
                phone,
                f"✅ Truck Listed!\n\n🆔 Listing ID: {listing_id}",
                [{"id": "MAIN_MENU", "title": "Main Menu"}]
            )
        
        else:
            await send_text(phone, "⚠️ No truck found. Please provide a truck plate.")

    elif p_id == "EDIT_TRUCK":
        update_session(db, phone, {"current_workflow": "TRUCK_FLOW"})
        await send_text(
            phone,
            "✏️ Let's update your truck details.\n\nTell me again (e.g. 'RJ14AB1234 10 ton Jaipur to Delhi tomorrow')"
        )

    elif p_id == "EDIT_LOAD":
        update_session(db, phone, {"current_workflow": "LOAD_FLOW"})
        await send_text(
            phone,
            "✏️ Let's update your load details.\n\nTell me again (e.g. '5 ton Jaipur to Delhi tomorrow')"
        )

    elif p_id == "TRACK_BOOKING":
        await send_text(phone, "📊 Enter your booking ID to track status.")

    elif p_id == "DELIVERY_STATUS":
        await send_text(phone, "🚚 Enter your booking ID to check delivery status.")

    elif p_id == "UPLOAD_KYC":
        await send_text(phone, "📄 Please upload your KYC document image.")
    
    elif p_id.startswith("SELECT_TRUCK_"):
        truck_id = p_id.replace("SELECT_TRUCK_", "")

        # 🔍 Fetch truck
        truck = db.query(TruckSpaceListing).filter(
            TruckSpaceListing.id == truck_id
        ).first()

        if not truck:
            await send_text(phone, "⚠️ Truck not found.")
            return
        # Store in session
        existing = get_session_data(db, phone, user.id) or {}

        set_session_data(db, phone, user.id, {
            **existing,
            "selected_truck_id": truck_id
        })
        # ✅ Create booking (simple version)
        booking_id = existing.get("booking_id", "UNKNOWN")

        await send_interactive_buttons(
            phone,
            f"🚚 Truck Selected!\n\n"
            f"📍 {truck.from_city} → {truck.to_city}\n"
            f"⚖️ {truck.available_capacity_kg} kg\n"
            f"💰 ₹{truck.price_per_kg}/kg\n\n"
            f"Booking ID: {booking_id}",
            [
                {"id": f"CONFIRM_BOOKING_{truck_id}", "title": "✅ Confirm Booking"},
                {"id": "EDIT_LOAD", "title": "✏️ Edit Load"},
                {"id": "MAIN_MENU", "title": "🏠 Main Menu"}
            ]
        )
    
    elif p_id.startswith("CONFIRM_BOOKING_"):

        data = get_session_data(db, phone, user.id) or {}

        truck_id = p_id.replace("CONFIRM_BOOKING_", "")
        selected_truck_id = data.get("selected_truck_id")

        if not selected_truck_id:
            await send_text(phone, "⚠️ No truck selected.")
            return

        # 🔥 Future: create Booking DB entry here

        clear_session(db, phone)

        await send_interactive_buttons(
            phone,
            "✅ Booking Confirmed!\n\nYour truck has been assigned.",
            [{"id": "MAIN_MENU", "title": "🏠 Main Menu"}]
        )


async def handle_new_user_onboarding(phone: str, user: User, text: str, msg_type: str, db: Session) -> bool:
    """Simple onboarding flow wrapper."""
    if not user.language:
        user.language = "en"
        db.commit()
        await send_text(phone, "Welcome! Language set to English.")
        await send_main_menu(phone)
        return True
    return False

async def handle_text_command(phone: str, user: User, text: str, db: Session) -> bool:
    """Interprets text commands."""
    t = text.lower().strip()
    if t.startswith("subscribe "):
        pts = t.split()
        if len(pts) >= 3:
            await subscribe_route(phone, user, pts[1], pts[2], db)
            return True
    return False