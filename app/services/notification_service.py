import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.services.recovery_service import RecoveryService
from app.services.matching_service import canonical_lane_key
from app.models.route_subscription import RouteSubscription
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.user import User
from app.runtime.redis_adapter import get_client as get_redis
from app.contracts.responses import Response

logger = logging.getLogger(__name__)

# Constants for notification rules
MATCH_THRESHOLD = 0.72
LANE_WATCH_RATE_LIMIT = 3  # max 3 per hour per lane per user
SUPPRESSION_TTL_MATCH = 86400  # 24h
RATE_LIMIT_TTL_LANE = 3600  # 1h

import time

class NotificationService:
    """
    Subscribes to EventBus events and handles multi-tier notification logic:
    1. MATCH_FOUND: Direct matches after high-score threshold.
    2. LOAD_CREATED / TRUCK_POSTED: Lane-watch subscriptions.
    """

    _delivery_enabled = True
    _last_redis_warning_at = 0
    _REDIS_WARNING_INTERVAL = 60

    @classmethod
    def configure(cls, settings):
        """Configure service-level toggles at startup."""
        if not getattr(settings, "WHATSAPP_TOKEN", None):
            logger.warning("[NOTIFY] WhatsApp delivery disabled (missing WHATSAPP_TOKEN)")
            cls._delivery_enabled = False
        else:
            cls._delivery_enabled = True
            logger.info("[NOTIFY] Notification delivery enabled")

    @classmethod
    def _should_suppress_redis_warning(cls) -> bool:
        now = time.time()
        if now - cls._last_redis_warning_at > cls._REDIS_WARNING_INTERVAL:
            cls._last_redis_warning_at = now
            return False
        return True

    @classmethod
    async def handle_match_found(cls, event: Dict[str, Any]):
        """
        Triggered when a potential match is identified.
        Filters by score threshold and applies duplicate suppression.
        """
        if not cls._delivery_enabled:
            return

        score = event.get("score", 0)
        if score < (MATCH_THRESHOLD * 100 if score > 1 else MATCH_THRESHOLD):
            return

        load_id = event.get("load_id")
        truck_id = event.get("listing_id")
        user_id = event.get("user_id") # The user being notified (transporter usually)
        
        if not all([load_id, truck_id, user_id]):
            return

        redis = get_redis()
        # Idempotency: match_notify:v1:{load}:{truck}:{user}
        suppression_key = f"match_notify:v1:{load_id}:{truck_id}:{user_id}"
        
        try:
            if redis.get(suppression_key):
                logger.info(f"[NOTIFY] Suppressing duplicate match notification: {suppression_key}")
                return
        except Exception:
            if not cls._should_suppress_redis_warning():
                logger.warning("[NOTIFY] Redis unavailable — match suppression bypassed")

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            load = db.query(LoadRequest).filter(LoadRequest.id == load_id).first()
            if not user or not load:
                return

            msg = (
                f"🔥 *New Match Found!*\n\n"
                f"Load: {load.from_city} → {load.to_city}\n"
                f"Weight: {load.weight_kg} kg\n"
                f"Date: {load.pickup_date.strftime('%d-%m-%Y') if load.pickup_date else 'Today'}\n\n"
                f"Reply with 'View' to see details."
            )
            
            recovery = RecoveryService(db)
            success = await recovery.send_with_backoff(user.phone, Response(text=msg))
            
            if success:
                try:
                    redis.set(suppression_key, "1", ex=SUPPRESSION_TTL_MATCH)
                except Exception:
                    # Fail-open: don't crash if we can't record the suppression
                    pass
                logger.info(f"[NOTIFY] Match notification sent to {user.phone}")
            
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to handle MATCH_FOUND event")
        finally:
            db.close()

    @classmethod
    async def handle_load_created(cls, event: Dict[str, Any]):
        """
        Triggered when a new load is committed to DB.
        Checks for matching lane-watch subscriptions.
        """
        if not cls._delivery_enabled:
            return

        load_id = event.get("load_id")
        if not load_id:
            return

        db = SessionLocal()
        try:
            load = db.query(LoadRequest).filter(LoadRequest.id == load_id).first()
            if not load:
                return

            lane_key = load.canonical_lane_key or canonical_lane_key(load.from_city, load.to_city)
            if not lane_key:
                return

            # Find active subscriptions for this exact lane
            subscriptions = db.query(RouteSubscription).filter(
                RouteSubscription.lane_key == lane_key
            ).all()

            if not subscriptions:
                return

            await cls._process_lane_watch_notifications(
                db, 
                subscriptions, 
                "New Load", 
                f"{load.from_city} → {load.to_city}",
                lane_key
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to handle LOAD_CREATED event")
        finally:
            db.close()

    @classmethod
    async def handle_truck_posted(cls, event: Dict[str, Any]):
        """
        Triggered when a new truck/listing is committed to DB.
        Checks for matching lane-watch subscriptions.
        """
        if not cls._delivery_enabled:
            return

        listing_id = event.get("listing_id")
        if not listing_id:
            return

        db = SessionLocal()
        try:
            listing = db.query(TruckSpaceListing).filter(TruckSpaceListing.id == listing_id).first()
            if not listing:
                return

            lane_key = listing.canonical_lane_key or canonical_lane_key(listing.from_city, listing.to_city)
            if not lane_key:
                return

            subscriptions = db.query(RouteSubscription).filter(
                RouteSubscription.lane_key == lane_key
            ).all()

            if not subscriptions:
                return

            await cls._process_lane_watch_notifications(
                db, 
                subscriptions, 
                "New Truck", 
                f"{listing.from_city} → {listing.to_city}",
                lane_key
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to handle TRUCK_POSTED event")
        finally:
            db.close()

    @classmethod
    async def _process_lane_watch_notifications(
        cls, 
        db: Session, 
        subscriptions: List[RouteSubscription], 
        entity_type: str, 
        route_display: str,
        lane_key: str
    ):
        redis = get_redis()
        recovery = RecoveryService(db)
        
        for sub in subscriptions:
            # Skip if user is the creator (usually)
            # In our case,subscriptions are often created by transporters watching for loads.
            
            # Rate Limit check: notification_rate:v1:{user_id}:{lane_key}
            rate_key = f"notification_rate:v1:{sub.user_id}:{lane_key}"
            try:
                current_count = redis.incr(rate_key)
                if current_count == 1:
                    redis.expire(rate_key, RATE_LIMIT_TTL_LANE)
                
                if current_count > LANE_WATCH_RATE_LIMIT:
                    logger.debug(f"[LANE_WATCH] Rate limit hit for user {sub.user_id} on {lane_key}")
                    continue
            except Exception:
                if not cls._should_suppress_redis_warning():
                    logger.warning("[NOTIFY] Redis unavailable — lane-watch rate limit bypassed")

            user = db.query(User).filter(User.id == sub.user_id).first()
            if not user or not user.phone:
                continue

            msg = f"🔔 *Lane Watch Alert*\n\n{entity_type} available on your watched route: *{route_display}*."
            
            # Non-blocking send (though this is already an async background task)
            await recovery.send_with_backoff(user.phone, Response(text=msg))
            
            # Update last_notified_at
            sub.last_notified_at = datetime.now(timezone.utc)
