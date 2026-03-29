"""Legacy chatbot-era formatting helpers kept only for compatibility review."""

# ---------------------------------------------------------------------------
# UI Helpers (Response Formatting)
# ---------------------------------------------------------------------------

def render_truck_confirmation(data: dict) -> tuple[str, list[dict]]:
    """
    Formats the truck listing details for user confirmation.
    Accepts both raw and normalized (canonical) keys for compatibility.
    """
    body = (
        f"🚚 Confirm Truck Listing\n\n"
        f"📍 {data.get('from_city', data.get('from', '?')).title()} → {data.get('to_city', data.get('to', '?')).title()}\n"
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

def render_load_confirmation(data: dict) -> tuple[str, list[dict]]:
    """
    Formats the load request details for user confirmation.
    """
    body = (
        f"📦 Confirm Load Request\n\n"
        f"📍 {data.get('from_city', data.get('from', '?')).title()} → {data.get('to_city', data.get('to', '?')).title()}\n"
        f"⚖️ {data.get('weight_kg', '?')} kg\n"
        f"📅 {data.get('date','?')}"
    )
    buttons = [
        {"id": "CONFIRM_LOAD_REQUEST", "title": "✅ Confirm"},
        {"id": "EDIT_LOAD", "title": "✏️ Edit"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"}
    ]
    return body, buttons
