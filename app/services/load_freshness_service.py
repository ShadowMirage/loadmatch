from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models.enums import LoadRequestStatus
from app.models.load_request import LoadRequest
from app.services.matching_service import load_canonical_lane_key, normalize_vehicle_type


def is_recent_duplicate_load(
    session,
    shipper_id,
    canonical_lane_key,
    vehicle_type,
    window_minutes,
) -> Optional[LoadRequest]:
    if not canonical_lane_key:
        return None

    normalized_vehicle_type = normalize_vehicle_type(vehicle_type)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    candidates = (
        session.query(LoadRequest)
        .filter(
            LoadRequest.shipper_id == shipper_id,
            LoadRequest.status.in_([LoadRequestStatus.open, LoadRequestStatus.matched]),
            LoadRequest.created_at >= cutoff,
        )
        .all()
    )

    for load in candidates:
        created_at = getattr(load, "created_at", None)
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                continue
        if load_canonical_lane_key(load) != canonical_lane_key:
            continue

        existing_vehicle_type = normalize_vehicle_type(getattr(load, "vehicle_type", None))
        if normalized_vehicle_type is None:
            if existing_vehicle_type is not None:
                continue
        elif existing_vehicle_type != normalized_vehicle_type:
            continue
        return load

    return None
