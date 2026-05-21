import logging
import asyncio
from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session
from app.services.event_bus import EventBus

from app.marketplace import LISTING_DUPLICATE_WINDOW_SECONDS, LISTING_FRESHNESS_TTL_SECONDS
from app.models.enums import ListingStatus, LoadRequestStatus, MatchStatus, KycFlowState, TruckType
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.match import Match
from app.models.user import User
from app.services.cargo_rules import is_cargo_compatible
from app.services.route_corridors import is_in_corridor, get_nearby_routes
from app.services.logistics_data import (
    CITY_LOGISTICS_HUBS, CorridorSource, get_corridor_source, normalize_hub_name
)

logger = logging.getLogger(__name__)


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _score_cache_bucket(score_context: dict | None, bucket_name: str) -> dict | None:
    if score_context is None:
        return None
    return score_context.setdefault(bucket_name, {})


def canonical_lane_key(origin: str, destination: str) -> str:
    normalized_origin = normalize_hub_name(origin or "")
    normalized_destination = normalize_hub_name(destination or "")
    if not normalized_origin or not normalized_destination:
        return ""
    ordered = sorted([normalized_origin, normalized_destination])
    return f"{ordered[0]}:{ordered[1]}"


def directional_lane_key(origin: str, destination: str) -> str:
    normalized_origin = normalize_hub_name(origin or "")
    normalized_destination = normalize_hub_name(destination or "")
    if not normalized_origin or not normalized_destination:
        return ""
    return f"{normalized_origin}->{normalized_destination}"


def listing_canonical_lane_key(listing: TruckSpaceListing) -> str:
    persisted_lane_key = getattr(listing, "canonical_lane_key", None)
    if persisted_lane_key:
        return persisted_lane_key
    logger.warning(
        "LANE_KEY_BACKFILL_FALLBACK_USED",
        extra={
            "listing_id": str(getattr(listing, "id", "")),
        },
    )
    logger.info(
        "PERSISTED_LANE_KEY_FALLBACK_USED",
        extra={
            "listing_id": str(getattr(listing, "id", "")),
            "from_city": getattr(listing, "from_city", None),
            "to_city": getattr(listing, "to_city", None),
        },
    )
    return canonical_lane_key(
        getattr(listing, "from_city", ""),
        getattr(listing, "to_city", ""),
    )


def load_canonical_lane_key(load: LoadRequest) -> str:
    persisted_lane_key = getattr(load, "canonical_lane_key", None)
    if persisted_lane_key:
        return persisted_lane_key
    return canonical_lane_key(
        getattr(load, "from_city", ""),
        getattr(load, "to_city", ""),
    )


def normalize_vehicle_type(value) -> str | None:
    raw_value = getattr(value, "value", value)
    normalized = str(raw_value or "").strip().lower().replace(" ", "_")
    if not normalized:
        return None
    for truck_type in TruckType:
        if truck_type.value == normalized:
            return truck_type.value
    return None


def load_vehicle_type(load: LoadRequest) -> str | None:
    return normalize_vehicle_type(getattr(load, "vehicle_type", None))


def is_listing_fresh(listing: TruckSpaceListing, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    created_at = _as_utc(getattr(listing, "created_at", None))
    if created_at is None:
        return True
    return created_at >= now - timedelta(seconds=LISTING_FRESHNESS_TTL_SECONDS)


def is_load_fresh(load: LoadRequest, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    created_at = _as_utc(getattr(load, "created_at", None))
    if created_at is None:
        return True
    return created_at >= now - timedelta(seconds=LISTING_FRESHNESS_TTL_SECONDS)


def find_recent_duplicate_listing(
    db: Session,
    owner_id,
    origin: str,
    destination: str,
    *,
    now: datetime | None = None,
) -> TruckSpaceListing | None:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=LISTING_DUPLICATE_WINDOW_SECONDS)
    target_lane_key = canonical_lane_key(origin, destination)
    if not target_lane_key:
        return None

    candidates = (
        db.query(TruckSpaceListing)
        .filter(
            TruckSpaceListing.owner_id == owner_id,
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
        if listing_canonical_lane_key(listing) == target_lane_key:
            return listing
    return None


def compute_lane_liquidity_score(
    session: Session | None,
    canonical_lane_key: str,
    *,
    vehicle_type: str | None = None,
    now: datetime | None = None,
    score_context: dict | None = None,
) -> int:
    if session is None or not canonical_lane_key:
        return 0

    normalized_vehicle_type = normalize_vehicle_type(vehicle_type)
    cache_key = (
        canonical_lane_key,
        normalized_vehicle_type or "__lane_level__",
    )
    bucket = _score_cache_bucket(score_context, "lane_liquidity")
    if bucket is not None and cache_key in bucket:
        return bucket[cache_key]
    count_bucket = _score_cache_bucket(score_context, "lane_liquidity_count")

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    base_query = (
        session.query(TruckSpaceListing)
        .filter(
            TruckSpaceListing.canonical_lane_key == canonical_lane_key,
            TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
            TruckSpaceListing.created_at >= cutoff,
        )
    )

    if normalized_vehicle_type is None:
        listing_count = base_query.count()
    else:
        typed_supply_count = base_query.filter(
            TruckSpaceListing.vehicle_type == normalized_vehicle_type
        ).count()
        if typed_supply_count > 0:
            listing_count = typed_supply_count
        else:
            typed_lane_supply_count = base_query.filter(
                TruckSpaceListing.vehicle_type.isnot(None)
            ).count()
            if typed_lane_supply_count == 0:
                listing_count = base_query.count()
            else:
                listing_count = 0

    score_delta = min(listing_count * 2, 20)
    if bucket is not None:
        bucket[cache_key] = score_delta
    if count_bucket is not None:
        count_bucket[cache_key] = listing_count
    logger.info(
        "LANE_LIQUIDITY_SCORE_APPLIED",
        extra={
            "lane_key": canonical_lane_key,
            "vehicle_type": normalized_vehicle_type,
            "listing_count": listing_count,
            "score_delta": score_delta,
        },
    )
    return score_delta


def compute_operator_reliability_bonus(
    session: Session | None,
    owner_id,
    *,
    now: datetime | None = None,
    score_context: dict | None = None,
) -> int:
    if session is None or not owner_id:
        return 0

    bucket = _score_cache_bucket(score_context, "operator_reliability")
    if bucket is not None and owner_id in bucket:
        return bucket[owner_id]

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    successful_match_count = (
        session.query(Match)
        .join(TruckSpaceListing, Match.listing_id == TruckSpaceListing.id)
        .filter(
            TruckSpaceListing.owner_id == owner_id,
            Match.status.in_([MatchStatus.confirmed, MatchStatus.completed]),
            Match.matched_at >= cutoff,
        )
        .count()
    )

    score_delta = 0
    if successful_match_count >= 6:
        score_delta = 10
    elif successful_match_count >= 3:
        score_delta = 6
    elif successful_match_count >= 1:
        score_delta = 3

    if bucket is not None:
        bucket[owner_id] = score_delta
    return score_delta


def compute_lane_specific_reliability_bonus(
    session: Session | None,
    owner_id,
    canonical_lane: str,
    *,
    now: datetime | None = None,
    score_context: dict | None = None,
) -> int:
    if session is None or not owner_id or not canonical_lane:
        return 0

    cache_key = (owner_id, canonical_lane)
    bucket = _score_cache_bucket(score_context, "lane_specific_reliability")
    if bucket is not None and cache_key in bucket:
        return bucket[cache_key]

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    successful_match_count = (
        session.query(Match)
        .join(TruckSpaceListing, Match.listing_id == TruckSpaceListing.id)
        .filter(
            TruckSpaceListing.owner_id == owner_id,
            TruckSpaceListing.canonical_lane_key == canonical_lane,
            Match.status.in_([MatchStatus.confirmed, MatchStatus.completed]),
            Match.matched_at >= cutoff,
        )
        .count()
    )

    score_delta = 0
    if successful_match_count >= 6:
        score_delta = 6
    elif successful_match_count >= 3:
        score_delta = 4
    elif successful_match_count >= 1:
        score_delta = 2

    if bucket is not None:
        bucket[cache_key] = score_delta
    return score_delta


def compute_lane_demand_score(
    session: Session | None,
    canonical_lane: str,
    *,
    vehicle_type: str | None = None,
    now: datetime | None = None,
    score_context: dict | None = None,
) -> int:
    if session is None or not canonical_lane:
        return 0

    normalized_vehicle_type = normalize_vehicle_type(vehicle_type)
    cache_key = (
        canonical_lane,
        normalized_vehicle_type or "__lane_level__",
    )
    bucket = _score_cache_bucket(score_context, "lane_demand")
    if bucket is not None and cache_key in bucket:
        return bucket[cache_key]

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    recent_open_loads_bucket = _score_cache_bucket(score_context, "recent_open_loads_24h")
    recent_open_loads = None
    if recent_open_loads_bucket is not None:
        recent_open_loads = recent_open_loads_bucket.get("rows")
    if recent_open_loads is None:
        recent_open_loads = (
            session.query(LoadRequest)
            .filter(
                LoadRequest.status == LoadRequestStatus.open,
                LoadRequest.created_at >= cutoff,
            )
            .all()
        )
        if recent_open_loads_bucket is not None:
            recent_open_loads_bucket["rows"] = recent_open_loads

    demand_count = 0
    lane_level_demand_count = 0
    typed_lane_demand_count = 0
    for load in recent_open_loads:
        created_at = _as_utc(getattr(load, "created_at", None))
        if created_at is not None and created_at < cutoff:
            continue
        if load_canonical_lane_key(load) != canonical_lane:
            continue
        lane_level_demand_count += 1
        load_type = load_vehicle_type(load)
        if load_type is not None:
            typed_lane_demand_count += 1
        if normalized_vehicle_type is None:
            demand_count += 1
            continue
        if load_type == normalized_vehicle_type:
            demand_count += 1

    # Legacy-safe fallback: if the target vehicle signal exists on listing side
    # but lane demand rows are still untyped, reuse lane-level demand until backfill catches up.
    if normalized_vehicle_type is not None and typed_lane_demand_count == 0:
        demand_count = lane_level_demand_count

    score_delta = min(demand_count * 2, 16)
    if bucket is not None:
        bucket[cache_key] = score_delta
    count_bucket = _score_cache_bucket(score_context, "lane_demand_count")
    if count_bucket is not None:
        count_bucket[cache_key] = demand_count
    return score_delta


def compute_supply_demand_ratio_score(
    session: Session | None,
    canonical_lane: str,
    *,
    vehicle_type: str | None = None,
    lane_demand_bonus: int | float | None = None,
    lane_liquidity_bonus: int | float | None = None,
    score_context: dict | None = None,
) -> int:
    if session is None or not canonical_lane:
        return 0

    normalized_vehicle_type = normalize_vehicle_type(vehicle_type)
    cache_key = (
        canonical_lane,
        normalized_vehicle_type or "__lane_level__",
    )
    bucket = _score_cache_bucket(score_context, "lane_supply_demand_ratio")
    if bucket is not None and cache_key in bucket:
        return bucket[cache_key]

    demand_count = None
    supply_count = None

    demand_count_bucket = _score_cache_bucket(score_context, "lane_demand_count")
    if demand_count_bucket is not None:
        demand_count = demand_count_bucket.get(cache_key)
    supply_count_bucket = _score_cache_bucket(score_context, "lane_liquidity_count")
    if supply_count_bucket is not None:
        supply_count = supply_count_bucket.get(cache_key)

    # If upstream scoring functions were mocked (tests) or not yet populated,
    # derive conservative count estimates from score deltas so this helper
    # stays side-effect-free and deterministic.
    if demand_count is None:
        demand_count = int(max((lane_demand_bonus or 0) / 2, 0))
    if supply_count is None:
        supply_count = int(max((lane_liquidity_bonus or 0) / 2, 0))

    ratio = demand_count / max(supply_count, 1)
    score_delta = min(20, int(ratio * 5))
    if bucket is not None:
        bucket[cache_key] = score_delta
    return score_delta


def compute_lane_activity_freshness_bonus(
    session: Session | None,
    canonical_lane: str,
    *,
    vehicle_type: str | None = None,
    now: datetime | None = None,
    score_context: dict | None = None,
) -> int:
    if session is None or not canonical_lane:
        return 0

    normalized_vehicle_type = normalize_vehicle_type(vehicle_type)
    cache_key = (
        canonical_lane,
        normalized_vehicle_type or "__lane_level__",
    )
    bucket = _score_cache_bucket(score_context, "lane_activity_freshness")
    if bucket is not None and cache_key in bucket:
        return bucket[cache_key]

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=6)
    recent_listings_bucket = _score_cache_bucket(score_context, "recent_open_listings_6h")
    recent_loads_bucket = _score_cache_bucket(score_context, "recent_open_loads_6h")

    recent_listings = None
    if recent_listings_bucket is not None:
        recent_listings = recent_listings_bucket.get("rows")
    if recent_listings is None:
        recent_listings = (
            session.query(TruckSpaceListing)
            .filter(
                TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
                TruckSpaceListing.created_at >= cutoff,
            )
            .all()
        )
        if recent_listings_bucket is not None:
            recent_listings_bucket["rows"] = recent_listings

    recent_loads = None
    if recent_loads_bucket is not None:
        recent_loads = recent_loads_bucket.get("rows")
    if recent_loads is None:
        recent_loads = (
            session.query(LoadRequest)
            .filter(
                LoadRequest.status == LoadRequestStatus.open,
                LoadRequest.created_at >= cutoff,
            )
            .all()
        )
        if recent_loads_bucket is not None:
            recent_loads_bucket["rows"] = recent_loads

    latest_lane_activity = None
    latest_vehicle_activity = None
    typed_lane_activity_count = 0

    for listing in recent_listings:
        created_at = _as_utc(getattr(listing, "created_at", None))
        if created_at is None:
            continue
        if created_at < cutoff:
            continue
        if listing_canonical_lane_key(listing) != canonical_lane:
            continue
        if latest_lane_activity is None or created_at > latest_lane_activity:
            latest_lane_activity = created_at
        listing_type = normalize_vehicle_type(getattr(listing, "vehicle_type", None))
        if listing_type is not None:
            typed_lane_activity_count += 1
        if normalized_vehicle_type is not None and listing_type == normalized_vehicle_type:
            if latest_vehicle_activity is None or created_at > latest_vehicle_activity:
                latest_vehicle_activity = created_at

    for load in recent_loads:
        created_at = _as_utc(getattr(load, "created_at", None))
        if created_at is None:
            continue
        if created_at < cutoff:
            continue
        if load_canonical_lane_key(load) != canonical_lane:
            continue
        if latest_lane_activity is None or created_at > latest_lane_activity:
            latest_lane_activity = created_at
        load_type = load_vehicle_type(load)
        if load_type is not None:
            typed_lane_activity_count += 1
        if normalized_vehicle_type is not None and load_type == normalized_vehicle_type:
            if latest_vehicle_activity is None or created_at > latest_vehicle_activity:
                latest_vehicle_activity = created_at

    latest_activity = latest_lane_activity
    if normalized_vehicle_type is not None:
        if latest_vehicle_activity is not None:
            latest_activity = latest_vehicle_activity
        elif typed_lane_activity_count > 0:
            latest_activity = None

    if latest_activity is None:
        if bucket is not None:
            bucket[cache_key] = 0
        return 0

    activity_age_minutes = (now - latest_activity).total_seconds() / 60
    score_delta = 0
    if activity_age_minutes <= 30:
        score_delta = 6
    elif activity_age_minutes <= 120:
        score_delta = 3
    elif activity_age_minutes <= 360:
        score_delta = 1
    if bucket is not None:
        bucket[cache_key] = score_delta
    return score_delta


def _trust_badge(owner: User) -> str:
    """Build a short trust badge string for a transporter."""
    parts = []
    if owner.rating:
        parts.append(f"⭐ {owner.rating:.1f}")
    if owner.kyc_flow_state == KycFlowState.verified:
        parts.append("✔ KYC")
    if owner.completed_trips:
        parts.append(f"{owner.completed_trips} trips")
    return " | ".join(parts) if parts else "New"

def corridor_bonus(origin: str, destination: str) -> int:
    """Awards differentiated bonus points based on corridor source quality."""
    if not origin or not destination:
        return 0
    
    # Normalize for comparison
    norm_origin = normalize_hub_name(origin)
    norm_dest = normalize_hub_name(destination)
    
    if not norm_origin or not norm_dest:
        return 0

    source = get_corridor_source(origin, destination)
    
    bonus_map = {
        CorridorSource.CITY_PAIR: 10,
        CorridorSource.INDUSTRIAL_ZONE_PAIR: 12,
        CorridorSource.ALIAS_PAIR: 8,
        CorridorSource.ADJACENT_CITY_PAIR: 9,
    }
    
    return bonus_map.get(source, 0)


def corridor_rank_bonus(corridor_source) -> int:
    if corridor_source is None:
        return 0

    raw_value = getattr(corridor_source, "value", corridor_source)
    normalized = str(raw_value or "").strip().lower()
    bonus_map = {
        "industrial_zone_pair": 12,
        "city_pair": 10,
        "adjacent_city_pair": 9,
        "alias_pair": 8,
    }
    return bonus_map.get(normalized, 0)


def compute_listing_recency_bonus(created_at, *, now: datetime | None = None) -> int:
    if created_at is None:
        return 0

    now = now or datetime.now(timezone.utc)
    created_at = _as_utc(created_at)
    age_minutes = (now - created_at).total_seconds() / 60

    if age_minutes <= 30:
        return 10
    if age_minutes <= 120:
        return 6
    if age_minutes <= 360:
        return 3
    if age_minutes <= 720:
        return 1
    return 0

def score_match(
    load: LoadRequest,
    listing: TruckSpaceListing,
    owner: User,
    session: Session | None = None,
    *,
    now: datetime | None = None,
    score_context: dict | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    score = 0
    route_score = 0
    structural_corridor_bonus = 0
    date_score = 0
    capacity_score = 0
    reputation_score = 0

    # 1. Route Score (Max 40)
    req_pickup = normalize_hub_name(load.from_city) if load.from_city else ""
    req_drop = normalize_hub_name(load.to_city) if load.to_city else ""
    list_pickup = normalize_hub_name(listing.from_city) if listing.from_city else ""
    list_drop = normalize_hub_name(listing.to_city) if listing.to_city else ""
    
    if req_pickup == list_pickup and req_drop == list_drop:
        route_score = 40
    elif is_in_corridor(req_pickup, req_drop, list_pickup, list_drop):
        route_score = 30
    elif req_pickup == list_pickup or req_drop == list_drop:
        route_score = 20
    score += route_score

    # Industrial Corridor Bonus (Reusing logistics_data)
    structural_corridor_bonus = corridor_bonus(load.from_city, load.to_city)
    score += structural_corridor_bonus

    # 2. Date Score (Max 20)
    if load.pickup_date and listing.departure_date:
        days_diff = abs((listing.departure_date - load.pickup_date).days)
        if days_diff == 0:
            date_score = 20
        elif days_diff == 1:
            date_score = 15
        elif days_diff == 2:
            date_score = 10
    score += date_score

    # 3. Capacity Score (Max 20)
    load_weight = getattr(load, 'weight_kg', 0) or 0
    avail_cap = getattr(listing, 'available_capacity_kg', 0) or 0
    
    if avail_cap == load_weight:
        capacity_score = 20
    elif load_weight < avail_cap <= (load_weight * 1.20):
        capacity_score = 15
    elif avail_cap > load_weight:
        capacity_score = 10
    score += capacity_score

    # 4. Reputation Score (Max 20)
    rep = owner.rating or 5.0
    if rep > 4.5:
        reputation_score = 20
    elif rep > 4.0:
        reputation_score = 15
    elif rep > 3.5:
        reputation_score = 10
    else:
        reputation_score = 5
    score += reputation_score

    rank_bonus = corridor_rank_bonus(getattr(load, "corridor_source", None))
    logger.info(
        "CORRIDOR_RANKING_APPLIED",
        extra={
            "lane_key": listing_canonical_lane_key(listing),
            "corridor_source": getattr(getattr(load, "corridor_source", None), "value", getattr(load, "corridor_source", None)),
            "score_delta": rank_bonus,
        },
    )
    score += rank_bonus

    created_at = _as_utc(getattr(listing, "created_at", None))
    recency_bonus = compute_listing_recency_bonus(created_at, now=now)
    age_minutes = None
    if created_at is not None:
        age_minutes = int((now - created_at).total_seconds() / 60)
    logger.info(
        "LISTING_RECENCY_SCORE_APPLIED",
        extra={
            "listing_id": str(getattr(listing, "id", "")),
            "age_minutes": age_minutes,
            "score_delta": recency_bonus,
        },
    )
    score += recency_bonus

    lane_key = listing_canonical_lane_key(listing)
    liquidity_score = compute_lane_liquidity_score(
        session,
        lane_key,
        vehicle_type=normalize_vehicle_type(getattr(listing, "vehicle_type", None)),
        now=now,
        score_context=score_context,
    )
    score += liquidity_score
    lane_demand_bonus = compute_lane_demand_score(
        session,
        lane_key,
        vehicle_type=normalize_vehicle_type(getattr(listing, "vehicle_type", None)),
        now=now,
        score_context=score_context,
    )
    score += lane_demand_bonus
    supply_demand_imbalance_bonus = compute_supply_demand_ratio_score(
        session,
        lane_key,
        vehicle_type=normalize_vehicle_type(getattr(listing, "vehicle_type", None)),
        lane_demand_bonus=lane_demand_bonus,
        lane_liquidity_bonus=liquidity_score,
        score_context=score_context,
    )
    score += supply_demand_imbalance_bonus
    lane_activity_freshness_bonus = compute_lane_activity_freshness_bonus(
        session,
        lane_key,
        vehicle_type=normalize_vehicle_type(getattr(listing, "vehicle_type", None)),
        now=now,
        score_context=score_context,
    )
    score += lane_activity_freshness_bonus
    reliability_bonus = compute_operator_reliability_bonus(
        session,
        getattr(listing, "owner_id", None),
        now=now,
        score_context=score_context,
    )
    score += reliability_bonus
    lane_specific_reliability_bonus = compute_lane_specific_reliability_bonus(
        session,
        getattr(listing, "owner_id", None),
        lane_key,
        now=now,
        score_context=score_context,
    )
    score += lane_specific_reliability_bonus

    vehicle_specific_score = (
        liquidity_score
        + lane_demand_bonus
        + supply_demand_imbalance_bonus
        + lane_activity_freshness_bonus
    )
    lane_score = (
        route_score
        + structural_corridor_bonus
        + rank_bonus
        + lane_specific_reliability_bonus
    )
    recency_score = recency_bonus
    operator_score = reputation_score + reliability_bonus

    final_score = min(score, 100)
    breakdown_bucket = _score_cache_bucket(score_context, "listing_breakdown")
    if breakdown_bucket is not None:
        breakdown_bucket[str(getattr(listing, "id", ""))] = {
            "vehicle_specific_score": vehicle_specific_score,
            "lane_score": lane_score,
            "recency_score": recency_score,
            "operator_score": operator_score,
        }
    logger.info(
        "RANKING_SCORE_BREAKDOWN",
        extra={
            "listing_id": str(getattr(listing, "id", "")),
            "lane_key": lane_key,
            "route_score": route_score,
            "structural_corridor_bonus": structural_corridor_bonus,
            "date_score": date_score,
            "capacity_score": capacity_score,
            "reputation_score": reputation_score,
            "corridor_bonus": rank_bonus,
            "liquidity_bonus": liquidity_score,
            "lane_demand_bonus": lane_demand_bonus,
            "supply_demand_imbalance_bonus": supply_demand_imbalance_bonus,
            "lane_activity_freshness_bonus": lane_activity_freshness_bonus,
            "recency_bonus": recency_bonus,
            "operator_reliability_bonus": reliability_bonus,
            "lane_specific_reliability_bonus": lane_specific_reliability_bonus,
            "vehicle_specific_score": vehicle_specific_score,
            "lane_score": lane_score,
            "recency_score": recency_score,
            "operator_score": operator_score,
            "final_score": final_score,
        },
    )

    return final_score


def _deterministic_match_sort_key(match_row: dict) -> tuple[float, float, float, float, float, str]:
    """Deterministic ranking contract for stable replay-equivalent ordering."""
    return (
        -float(match_row.get("score", 0)),
        -float(match_row.get("_vehicle_specific_score", 0)),
        -float(match_row.get("_lane_score", 0)),
        -float(match_row.get("_recency_score", 0)),
        -float(match_row.get("_operator_score", 0)),
        str(match_row.get("_listing_id", "")),
    )


def rank_matches(
    load: LoadRequest,
    trucks: list,
    db: Session,
    *,
    now: datetime | None = None,
) -> list[dict]:
    scoring_now = now or datetime.now(timezone.utc)
    score_context: dict = {}
    match_results = []
    
    for listing, owner in trucks:
        score = score_match(load, listing, owner, db, now=scoring_now, score_context=score_context)
        
        # Emit event for background notification processing (Notify Transporters)
        eb = EventBus("background-matching")
        asyncio.create_task(eb.emit_async({
            "event": "MATCH_FOUND",
            "load_id": str(load.id),
            "listing_id": str(listing.id),
            "user_id": str(listing.owner_id),
            "score": score / 100.0 if score > 1 else score,
            "pii_redact": True
        }))
        
        # Upsert Match record for DB tracking
        match = (
            db.query(Match)
            .filter(Match.listing_id == listing.id, Match.load_request_id == load.id)
            .first()
        )
        if match:
            match.match_score = score
        else:
            match = Match(
                listing_id=listing.id,
                load_request_id=load.id,
                match_score=score,
                status=MatchStatus.suggested
            )
            db.add(match)
            
        # We collect the match object to get the ID after flush
        match_results.append({
            "match_obj":            match,
            "score":                float(score),
            "owner":                owner,
            "listing":              listing
        })
    
    # Consolidate disk syncs
    db.flush()

    final_results = []
    breakdown_bucket = score_context.get("listing_breakdown") if isinstance(score_context, dict) else {}
    for m in match_results:
        match = m["match_obj"]
        owner = m["owner"]
        listing = m["listing"]
        listing_id = str(getattr(listing, "id", ""))
        breakdown = breakdown_bucket.get(listing_id, {}) if isinstance(breakdown_bucket, dict) else {}
        
        final_results.append({
            "match_id":             str(match.id),
            "score":                m["score"],
            "match_score_pct":      int(m["score"]),              
            "trust_badge":          _trust_badge(owner),
            "from_city":            listing.from_city,
            "to_city":              listing.to_city,
            "departure_date":       listing.departure_date.isoformat(),
            "available_capacity_kg": listing.available_capacity_kg,
            "price_per_kg":         float(listing.price_per_kg) if listing.price_per_kg else 0.0,
            "owner_name":           owner.name,
            "owner_phone":          owner.phone,
            "owner_rating":         float(owner.rating) if owner.rating else None,
            "completed_trips":      owner.completed_trips,
            "_vehicle_specific_score": float(breakdown.get("vehicle_specific_score", 0)),
            "_lane_score": float(breakdown.get("lane_score", 0)),
            "_recency_score": float(breakdown.get("recency_score", 0)),
            "_operator_score": float(breakdown.get("operator_score", 0)),
            "_listing_id": listing_id,
        })
        
    # Deterministic tie-break hierarchy:
    # score > vehicle_specific > lane > recency > operator > listing_id
    final_results.sort(key=_deterministic_match_sort_key)
    logger.info(
        "MATCH_ORDER_DECISION_TRACE",
        extra={
            "lane_key": load_canonical_lane_key(load),
            "candidate_ids": [result["match_id"] for result in final_results],
        },
    )
    for result in final_results:
        result.pop("_vehicle_specific_score", None)
        result.pop("_lane_score", None)
        result.pop("_recency_score", None)
        result.pop("_operator_score", None)
        result.pop("_listing_id", None)
    return final_results[:5]

def suggest_nearby_matches(load: LoadRequest) -> list[dict]:
    """Smart suggestions when no exact match is found."""
    nearby_routes = get_nearby_routes(load.from_city, load.to_city)
    if nearby_routes:
        return [{"fallback_suggestions": nearby_routes}]
    return []


def find_matches_for_load(
    db: Session,
    load: LoadRequest,
    commit: bool = False,
    *,
    now: datetime | None = None,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    start_date = load.pickup_date - timedelta(days=2)
    end_date   = load.pickup_date + timedelta(days=2)
    freshness_cutoff = now - timedelta(seconds=LISTING_FRESHNESS_TTL_SECONDS)

    candidates = (
        db.query(TruckSpaceListing, User)
        .join(User, TruckSpaceListing.owner_id == User.id)
        .filter(
            TruckSpaceListing.departure_date >= start_date,
            TruckSpaceListing.departure_date <= end_date,
            TruckSpaceListing.available_capacity_kg >= load.weight_kg,
            TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
            TruckSpaceListing.created_at >= freshness_cutoff,
            (TruckSpaceListing.expires_at == None) | (TruckSpaceListing.expires_at > now),
        )
        .all()
    )

    valid_trucks = []
    for listing, owner in candidates:
        if not is_listing_fresh(listing, now=now):
            continue
        if str(listing.owner_id) == str(load.shipper_id):
            continue
        if not is_cargo_compatible(load.category, listing.allowed_categories):
            continue
        valid_trucks.append((listing, owner))
        
    match_results = rank_matches(load, valid_trucks, db, now=now)
    # Smart Suggestions Logic
    if not match_results:
        match_results = suggest_nearby_matches(load)

    if commit:
        db.commit()
    return match_results


def update_listing_capacity_after_match(db: Session, match_id: str, commit: bool = False):
    match   = db.query(Match).filter(Match.id == match_id).first()
    if not match:
        raise ValueError("Match not found")

    listing = db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match.listing_id).first()
    load    = db.query(LoadRequest).filter(LoadRequest.id == match.load_request_id).first()

    if not listing or not load:
        raise ValueError("Listing or LoadRequest not found")
    if listing.available_capacity_kg < load.weight_kg:
        raise ValueError("Not enough capacity in the listing")

    listing.available_capacity_kg -= load.weight_kg
    listing.status = ListingStatus.full if listing.available_capacity_kg <= 0 else ListingStatus.partial
    load.status    = LoadRequestStatus.confirmed
    match.status   = MatchStatus.accepted
    if commit:
        db.commit()
    else:
        db.flush()


# ---------------------------------------------------------------------------
# High-Level Helpers for Live Match Feedback
# ---------------------------------------------------------------------------

def find_matches_for_truck_summary(
    db: Session,
    listing: TruckSpaceListing,
    *,
    now: datetime | None = None,
) -> dict:
    """
    Find up to 3 matching loads for a truck listing.
    Returns a summarized dictionary for UI display.
    """
    now = now or datetime.now(timezone.utc)
    # Search loads within +/- 2 days of departure
    start_date = listing.departure_date - timedelta(days=2)
    end_date   = listing.departure_date + timedelta(days=2)
    freshness_cutoff = now - timedelta(seconds=LISTING_FRESHNESS_TTL_SECONDS)

    candidates = (
        db.query(LoadRequest, User)
        .join(User, LoadRequest.shipper_id == User.id)
        .filter(
            LoadRequest.pickup_date >= start_date,
            LoadRequest.pickup_date <= end_date,
            LoadRequest.weight_kg <= listing.available_capacity_kg,
            LoadRequest.status == LoadRequestStatus.open,
            LoadRequest.created_at >= freshness_cutoff,
            (LoadRequest.expires_at == None) | (LoadRequest.expires_at > now)
        )
        .all()
    )

    matches = []
    for load, shipper in candidates:
        if not is_load_fresh(load, now=now):
            continue
        if str(load.shipper_id) == str(listing.owner_id):
            continue
        if not is_in_corridor(load.from_city, load.to_city, listing.from_city, listing.to_city):
            continue
        if not is_cargo_compatible(load.category, listing.allowed_categories):
            continue

        # 🔥 AUTHORITATIVE SCORING (Unified)
        score_pct = score_match(load, listing, shipper, db, now=now)

        # Emit event for background notification processing
        # We pass trace_id if available, though here we might need to rely on system trace
        eb = EventBus("background-matching")
        asyncio.create_task(eb.emit_async({
            "event": "MATCH_FOUND",
            "load_id": str(load.id),
            "listing_id": str(listing.id),
            "user_id": str(load.shipper_id),
            "score": score_pct / 100.0 if score_pct > 1 else score_pct,
            "pii_redact": True
        }))

        matches.append({
            "cargo":       load.category.value if hasattr(load.category, 'value') else str(load.category),
            "weight":      load.weight_kg,
            "pickup":      load.from_city,
            "drop":        load.to_city,
            "match_score": int(score_pct),
            "load_id":     str(load.id)
        })

    matches.sort(key=lambda x: (-x["match_score"], x["load_id"]))
    top_matches = matches[:3]

    return {
        "match_count": len(matches),
        "matches": top_matches
    }


def find_matches_for_load_summary(
    db: Session,
    load: LoadRequest,
    commit: bool = False,
    *,
    now: datetime | None = None,
) -> dict:
    """
    Find up to 3 matching trucks for a load request.
    Returns a summarized dictionary for UI display.
    """
    results = find_matches_for_load(db, load, commit=commit, now=now)
    
    # Check for fallback suggestions structure
    if results and "fallback_suggestions" in results[0]:
        return {
            "match_count": 0,
            "matches": [],
            "fallback_suggestions": results[0]["fallback_suggestions"]
        }

    matches = []
    for m in results[:3]:
        matches.append({
            "cargo":       "Truck", # For loads, we show the truck info
            "weight":      m["available_capacity_kg"],
            "pickup":      m["from_city"],
            "drop":        m["to_city"],
            "match_score": int(m["match_score_pct"])
        })

    return {
        "match_count": len(results),
        "matches": matches
    }
