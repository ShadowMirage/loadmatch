from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session

from app.models.enums import ListingStatus, LoadRequestStatus, MatchStatus, KycFlowState
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.match import Match
from app.models.user import User
from app.services.cargo_rules import is_cargo_compatible
from app.services.route_corridors import is_in_corridor, get_nearby_routes


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

def score_match(load: LoadRequest, listing: TruckSpaceListing, owner: User) -> int:
    score = 0
    
    # 1. Route Score (Max 40)
    req_pickup = load.from_city.lower() if load.from_city else ""
    req_drop = load.to_city.lower() if load.to_city else ""
    list_pickup = listing.from_city.lower() if listing.from_city else ""
    list_drop = listing.to_city.lower() if listing.to_city else ""
    
    if req_pickup == list_pickup and req_drop == list_drop:
        score += 40
    elif is_in_corridor(req_pickup, req_drop, list_pickup, list_drop):
        score += 30
    elif req_pickup == list_pickup or req_drop == list_drop:
        score += 20
        
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
            
        db.flush()
        
        match_results.append({
            "match_id":             str(match.id),
            "score":                float(score),
            "match_score_pct":      int(score),              
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
        
    match_results.sort(key=lambda x: x["score"], reverse=True)
    return match_results[:5]

def suggest_nearby_matches(load: LoadRequest) -> list[dict]:
    """Smart suggestions when no exact match is found."""
    nearby_routes = get_nearby_routes(load.from_city, load.to_city)
    if nearby_routes:
        return [{"fallback_suggestions": nearby_routes}]
    return []


def find_matches_for_load(db: Session, load: LoadRequest) -> list[dict]:
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

    db.commit()
    return match_results


def update_listing_capacity_after_match(db: Session, match_id: str):
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
    db.commit()


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

        # Simple score for live feedback
        score = 80 # Base for corridor match
        days_diff = abs((listing.departure_date - load.pickup_date).days)
        score += (20 - (days_diff * 10))
        score = min(score, 100)

        matches.append({
            "cargo":       load.category.value if hasattr(load.category, 'value') else str(load.category),
            "weight":      load.weight_kg,
            "pickup":      load.from_city,
            "drop":        load.to_city,
            "match_score": int(score)
        })

    matches.sort(key=lambda x: x["match_score"], reverse=True)
    top_matches = matches[:3]

    return {
        "match_count": len(matches),
        "matches": top_matches
    }


def find_matches_for_load_summary(db: Session, load: LoadRequest) -> dict:
    """
    Find up to 3 matching trucks for a load request.
    Returns a summarized dictionary for UI display.
    """
    results = find_matches_for_load(db, load)
    
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
