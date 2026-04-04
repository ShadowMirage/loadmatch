from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session

from app.models.enums import ListingStatus, LoadRequestStatus, MatchStatus, KycFlowState
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.match import Match
from app.models.user import User
from app.services.cargo_rules import is_cargo_compatible
from app.services.route_corridors import is_in_corridor, get_nearby_routes
from app.services.logistics_data import (
    CITY_LOGISTICS_HUBS, CorridorSource, get_corridor_source, normalize_hub_name
)


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

def score_match(load: LoadRequest, listing: TruckSpaceListing, owner: User) -> int:
    score = 0
    
    # 1. Route Score (Max 40)
    req_pickup = normalize_hub_name(load.from_city) if load.from_city else ""
    req_drop = normalize_hub_name(load.to_city) if load.to_city else ""
    list_pickup = normalize_hub_name(listing.from_city) if listing.from_city else ""
    list_drop = normalize_hub_name(listing.to_city) if listing.to_city else ""
    
    if req_pickup == list_pickup and req_drop == list_drop:
        score += 40
    elif is_in_corridor(req_pickup, req_drop, list_pickup, list_drop):
        score += 30
    elif req_pickup == list_pickup or req_drop == list_drop:
        score += 20
    
    # Industrial Corridor Bonus (Reusing logistics_data)
    score += corridor_bonus(load.from_city, load.to_city)
        
    # 2. Date Score (Max 20)
    if load.pickup_date and listing.departure_date:
        days_diff = abs((listing.departure_date - load.pickup_date).days)
        if days_diff == 0:
            score += 20
        elif days_diff == 1:
            score += 15
        elif days_diff == 2:
            score += 10
            
    # 3. Capacity Score (Max 20)
    load_weight = getattr(load, 'weight_kg', 0) or 0
    avail_cap = getattr(listing, 'available_capacity_kg', 0) or 0
    
    if avail_cap == load_weight:
        score += 20
    elif load_weight < avail_cap <= (load_weight * 1.20):
        score += 15
    elif avail_cap > load_weight:
        score += 10
        
    # 4. Reputation Score (Max 20)
    rep = owner.rating or 5.0
    if rep > 4.5:
        score += 20
    elif rep > 4.0:
        score += 15
    elif rep > 3.5:
        score += 10
    else:
        score += 5
        
    return min(score, 100)

def rank_matches(load: LoadRequest, trucks: list, db: Session) -> list[dict]:
    match_results = []
    
    for listing, owner in trucks:
        score = score_match(load, listing, owner)
        
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
    for m in match_results:
        match = m["match_obj"]
        owner = m["owner"]
        listing = m["listing"]
        
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
        })
        
    # 🔥 Deterministic tie-breaker: sort by score DESC, then match_id ASC
    final_results.sort(key=lambda x: (-x["score"], x["match_id"]))
    return final_results[:5]

def suggest_nearby_matches(load: LoadRequest) -> list[dict]:
    """Smart suggestions when no exact match is found."""
    nearby_routes = get_nearby_routes(load.from_city, load.to_city)
    if nearby_routes:
        return [{"fallback_suggestions": nearby_routes}]
    return []


def find_matches_for_load(db: Session, load: LoadRequest, commit: bool = False) -> list[dict]:
    now = datetime.now(timezone.utc)
    start_date = load.pickup_date - timedelta(days=2)
    end_date   = load.pickup_date + timedelta(days=2)

    candidates = (
        db.query(TruckSpaceListing, User)
        .join(User, TruckSpaceListing.owner_id == User.id)
        .filter(
            TruckSpaceListing.departure_date >= start_date,
            TruckSpaceListing.departure_date <= end_date,
            TruckSpaceListing.available_capacity_kg >= load.weight_kg,
            TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
            (TruckSpaceListing.expires_at == None) | (TruckSpaceListing.expires_at > now),
        )
        .all()
    )

    valid_trucks = []
    for listing, owner in candidates:
        if not is_cargo_compatible(load.category, listing.allowed_categories):
            continue
        valid_trucks.append((listing, owner))
        
    match_results = rank_matches(load, valid_trucks, db)
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

def find_matches_for_truck_summary(db: Session, listing: TruckSpaceListing) -> dict:
    """
    Find up to 3 matching loads for a truck listing.
    Returns a summarized dictionary for UI display.
    """
    now = datetime.now(timezone.utc)
    # Search loads within +/- 2 days of departure
    start_date = listing.departure_date - timedelta(days=2)
    end_date   = listing.departure_date + timedelta(days=2)

    candidates = (
        db.query(LoadRequest, User)
        .join(User, LoadRequest.shipper_id == User.id)
        .filter(
            LoadRequest.pickup_date >= start_date,
            LoadRequest.pickup_date <= end_date,
            LoadRequest.weight_kg <= listing.available_capacity_kg,
            LoadRequest.status == LoadRequestStatus.open,
            (LoadRequest.expires_at == None) | (LoadRequest.expires_at > now)
        )
        .all()
    )

    matches = []
    for load, shipper in candidates:
        if not is_in_corridor(load.from_city, load.to_city, listing.from_city, listing.to_city):
            continue
        if not is_cargo_compatible(load.category, listing.allowed_categories):
            continue

        # 🔥 AUTHORITATIVE SCORING (Unified)
        score = score_match(load, listing, shipper)

        matches.append({
            "cargo":       load.category.value if hasattr(load.category, 'value') else str(load.category),
            "weight":      load.weight_kg,
            "pickup":      load.from_city,
            "drop":        load.to_city,
            "match_score": int(score),
            "load_id":     str(load.id)
        })

    matches.sort(key=lambda x: (-x["match_score"], x["load_id"]))
    top_matches = matches[:3]

    return {
        "match_count": len(matches),
        "matches": top_matches
    }


def find_matches_for_load_summary(db: Session, load: LoadRequest, commit: bool = False) -> dict:
    """
    Find up to 3 matching trucks for a load request.
    Returns a summarized dictionary for UI display.
    """
    results = find_matches_for_load(db, load, commit=commit)
    
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
