from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models.enums import ListingStatus
from app.models.listing import TruckSpaceListing
from app.services.matching_service import listing_canonical_lane_key


def is_recent_duplicate_lane(
    session,
    owner_id,
    canonical_lane_key,
    vehicle_type,
    window_minutes,
) -> Optional[TruckSpaceListing]:
    if not canonical_lane_key or not vehicle_type:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    candidates = (
        session.query(TruckSpaceListing)
        .filter(
            TruckSpaceListing.owner_id == owner_id,
            TruckSpaceListing.vehicle_type == vehicle_type,
            TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
            TruckSpaceListing.created_at >= cutoff,
        )
        .all()
    )

    for listing in candidates:
        created_at = getattr(listing, "created_at", None)
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                continue
        if listing_canonical_lane_key(listing) == canonical_lane_key:
            return listing

    return None
