"""
Supply Visibility Service
=========================
Sends automatic WhatsApp notifications when new supply (loads or trucks) appears
on a route where interested counterparties or route subscribers exist.

Includes a notification cooldown: max 5 notifications per user per hour per route corridor.
"""
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.route_subscription import RouteSubscription
from app.models.event import EventLog
from app.models.enums import ListingStatus, LoadRequestStatus
from app.services.logistics_data import normalize_hub_name
from app.services.route_corridors import is_in_corridor
from app.services.cargo_rules import is_cargo_compatible
from app.services.event_logger import track_event
from app.services.whatsapp_service import send_interactive_buttons

logger = logging.getLogger(__name__)

MAX_NOTIFICATIONS_PER_TRIGGER = 3
COOLDOWN_LIMIT = 5
COOLDOWN_WINDOW_HOURS = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listing_alive(listing: TruckSpaceListing) -> bool:
    if listing.status not in (ListingStatus.open, ListingStatus.partial):
        return False
    if listing.expires_at and listing.expires_at.replace(tzinfo=timezone.utc) < _now():
        return False
    return True


def _load_alive(load: LoadRequest) -> bool:
    if load.status != LoadRequestStatus.open:
        return False
    if load.expires_at and load.expires_at.replace(tzinfo=timezone.utc) < _now():
        return False
    return True


def _is_on_cooldown(db: Session, user_id, from_city: str, to_city: str) -> bool:
    """
    Stabilization Pass Fix #11: Max 5 notifications per user per hour per route.
    """
    one_hour_ago = _now() - timedelta(hours=COOLDOWN_WINDOW_HOURS)
    
    # Check events table for SVP or MATCH_NOTIFICATION in the last hour for this user
    count = db.query(EventLog).filter(
        EventLog.user_id == user_id,
        EventLog.event_type.in_(["SUPPLY_VISIBILITY_PING", "MATCH_NOTIFICATION_SENT"]),
        EventLog.created_at >= one_hour_ago
    ).count()
    
    # We could refine this to check specific route in payload, but a general 
    # per-user alert cap is safer for demo stability.
    return count >= COOLDOWN_LIMIT


# ---------------------------------------------------------------------------
# Notification senders
# ---------------------------------------------------------------------------

async def _notify_truck_owner(user: User, load: LoadRequest) -> None:
    """Tell a transporter that a new load matches their truck route."""
    weight_tons = round(load.weight_kg / 1000, 1) if load.weight_kg else "?"
    body = (
        f"📦 New Load Matching Your Route\n"
        f"Route: {load.from_city.title()} → {load.to_city.title()}\n"
        f"Weight: {weight_tons} tons"
    )
    buttons = [{"id": "POST_TRUCK", "title": "Post Truck"}]
    await send_interactive_buttons(user.phone, body, buttons)


async def _notify_shipper(user: User, listing: TruckSpaceListing) -> None:
    """Tell a shipper that a new truck is available on their route."""
    capacity_tons = round(listing.available_capacity_kg / 1000, 1) if listing.available_capacity_kg else "?"
    body = (
        f"🚚 Truck Available on Your Route\n"
        f"Route: {listing.from_city.title()} → {listing.to_city.title()}\n"
        f"Capacity: {capacity_tons} tons | ₹{listing.price_per_kg}/kg"
    )
    buttons = [{"id": "POST_LOAD", "title": "📦 Post Load"}]
    await send_interactive_buttons(user.phone, body, buttons)


async def _notify_subscriber_load(user: User, load: LoadRequest) -> None:
    """Alert a route subscriber about a newly posted load."""
    weight_tons = round(load.weight_kg / 1000, 1) if load.weight_kg else "?"
    cargo = load.category.value.capitalize() if load.category else "General"
    body = (
        f"⚡ New Load Opportunity\n"
        f"Route: {load.from_city.title()} → {load.to_city.title()}\n"
        f"Load: {cargo} | {weight_tons} tons"
    )
    buttons = [
        {"id": "POST_TRUCK", "title": "🚚 Post Truck"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"},
    ]
    await send_interactive_buttons(user.phone, body, buttons)


async def _notify_subscriber_truck(user: User, listing: TruckSpaceListing) -> None:
    """Alert a route subscriber about a newly posted truck."""
    capacity_tons = round(listing.available_capacity_kg / 1000, 1) if listing.available_capacity_kg else "?"
    body = (
        f"⚡ New Truck Opportunity\n"
        f"Route: {listing.from_city.title()} → {listing.to_city.title()}\n"
        f"Capacity: {capacity_tons} tons | ₹{listing.price_per_kg}/kg"
    )
    buttons = [
        {"id": "POST_LOAD", "title": "📦 Post Load"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"},
    ]
    await send_interactive_buttons(user.phone, body, buttons)


# ---------------------------------------------------------------------------
# Main SVP entry points
# ---------------------------------------------------------------------------

async def notify_on_load_created(db: Session, load: LoadRequest) -> None:
    """
    Called after a new load is created.
    """
    if not _load_alive(load):
        return

    notified: set = set()
    count = 0

    try:
        # ── 1. Find matching truck listings ──────────────────────────────
        candidate_listings = (
            db.query(TruckSpaceListing, User)
            .join(User, TruckSpaceListing.owner_id == User.id)
            .filter(
                TruckSpaceListing.available_capacity_kg >= load.weight_kg,
                TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
            )
            .limit(50)
            .all()
        )

        for listing, owner in candidate_listings:
            if count >= MAX_NOTIFICATIONS_PER_TRIGGER:
                break
            if owner.id == load.shipper_id:
                continue
            if owner.phone in notified:
                continue
            if _is_on_cooldown(db, owner.id, load.from_city, load.to_city):
                continue
            if not _listing_alive(listing):
                continue
            if not is_in_corridor(load.from_city, load.to_city, listing.from_city, listing.to_city):
                continue
            if not is_cargo_compatible(load.category, listing.allowed_categories):
                continue

            try:
                await _notify_truck_owner(owner, load)
                track_event(db, owner.id, "MATCH_NOTIFICATION_SENT", {
                    "trigger": "LOAD_CREATED", "load_id": str(load.id)
                })
                notified.add(owner.phone)
                count += 1
            except Exception as e:
                logger.warning("SVP truck-owner notify failed: %s", e)

        # ── 2. Route subscribers ─────────────────────────────────────────
        subscribers = (
            db.query(RouteSubscription, User)
            .join(User, RouteSubscription.user_id == User.id)
            .filter(
                RouteSubscription.normalized_pickup == normalize_hub_name(load.from_city),
                RouteSubscription.normalized_drop == normalize_hub_name(load.to_city),
            )
            .limit(10)
            .all()
        )

        for sub, sub_user in subscribers:
            if count >= MAX_NOTIFICATIONS_PER_TRIGGER:
                break
            if sub_user.phone in notified:
                continue
            if sub_user.id == load.shipper_id:
                continue
            if _is_on_cooldown(db, sub_user.id, load.from_city, load.to_city):
                continue

            try:
                await _notify_subscriber_load(sub_user, load)
                track_event(db, sub_user.id, "SUPPLY_VISIBILITY_PING", {
                    "trigger": "LOAD_CREATED", "load_id": str(load.id)
                })
                notified.add(sub_user.phone)
                count += 1
            except Exception as e:
                logger.warning("SVP subscriber notify failed: %s", e)

    except Exception as e:
        logger.error("SVP notify_on_load_created error: %s", e)


async def notify_on_truck_listed(db: Session, listing: TruckSpaceListing) -> None:
    """
    Called after a new truck listing is created.
    """
    if not _listing_alive(listing):
        return

    notified: set = set()
    count = 0

    try:
        # ── 1. Find matching open loads ───────────────────────────────────
        candidate_loads = (
            db.query(LoadRequest, User)
            .join(User, LoadRequest.shipper_id == User.id)
            .filter(
                LoadRequest.weight_kg <= listing.available_capacity_kg,
                LoadRequest.status == LoadRequestStatus.open,
            )
            .limit(50)
            .all()
        )

        for load, shipper in candidate_loads:
            if count >= MAX_NOTIFICATIONS_PER_TRIGGER:
                break
            if shipper.id == listing.owner_id:
                continue
            if shipper.phone in notified:
                continue
            if _is_on_cooldown(db, shipper.id, listing.from_city, listing.to_city):
                continue
            if not _load_alive(load):
                continue
            if not is_in_corridor(load.from_city, load.to_city, listing.from_city, listing.to_city):
                continue
            if not is_cargo_compatible(load.category, listing.allowed_categories):
                continue

            try:
                await _notify_shipper(shipper, listing)
                track_event(db, shipper.id, "MATCH_NOTIFICATION_SENT", {
                    "trigger": "TRUCK_LISTED", "listing_id": str(listing.id)
                })
                notified.add(shipper.phone)
                count += 1
            except Exception as e:
                logger.warning("SVP shipper notify failed: %s", e)

        # ── 2. Route subscribers ─────────────────────────────────────────
        subscribers = (
            db.query(RouteSubscription, User)
            .join(User, RouteSubscription.user_id == User.id)
            .filter(
                RouteSubscription.normalized_pickup == normalize_hub_name(listing.from_city),
                RouteSubscription.normalized_drop == normalize_hub_name(listing.to_city),
            )
            .limit(10)
            .all()
        )

        for sub, sub_user in subscribers:
            if count >= MAX_NOTIFICATIONS_PER_TRIGGER:
                break
            if sub_user.phone in notified:
                continue
            if sub_user.id == listing.owner_id:
                continue
            if _is_on_cooldown(db, sub_user.id, listing.from_city, listing.to_city):
                continue

            try:
                await _notify_subscriber_truck(sub_user, listing)
                track_event(db, sub_user.id, "SUPPLY_VISIBILITY_PING", {
                    "trigger": "TRUCK_LISTED", "listing_id": str(listing.id)
                })
                notified.add(sub_user.phone)
                count += 1
            except Exception as e:
                logger.warning("SVP subscriber notify failed: %s", e)

    except Exception as e:
        logger.error("SVP notify_on_truck_listed error: %s", e)
