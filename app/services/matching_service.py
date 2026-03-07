from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.listing import TruckSpaceListing, ListingStatus
from app.models.load_request import LoadRequest, LoadRequestStatus
from app.models.match import Match, MatchStatus
from app.models.user import User, KycStatus

def find_matches_for_load(db: Session, load: LoadRequest) -> list[dict]:
    # 1. Query candidate listings
    start_date = load.pickup_date - timedelta(days=2)
    end_date = load.pickup_date + timedelta(days=2)
    
    candidates = (
        db.query(TruckSpaceListing, User)
        .join(User, TruckSpaceListing.owner_id == User.id)
        .filter(
            func.lower(TruckSpaceListing.from_city) == load.from_city.lower(),
            func.lower(TruckSpaceListing.to_city) == load.to_city.lower(),
            TruckSpaceListing.departure_date >= start_date,
            TruckSpaceListing.departure_date <= end_date,
            TruckSpaceListing.available_capacity_kg >= load.weight_kg,
            TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial])
        )
        .all()
    )
    
    match_results = []
    
    for listing, owner in candidates:
        score = 0
        
        # Date proximity
        days_diff = abs((listing.departure_date - load.pickup_date).days)
        if days_diff == 0:
            score += 30
        elif days_diff == 1:
            score += 20
        elif days_diff == 2:
            score += 10
            
        # Capacity fit
        capacity_ratio = load.weight_kg / listing.available_capacity_kg
        if capacity_ratio >= 0.5:
            score += 30
        else:
            score += 15
            
        # Price within budget
        if load.budget_per_kg is None:
            score += 10
        else:
            if float(listing.price_per_kg) <= float(load.budget_per_kg):
                score += 20
            elif float(listing.price_per_kg) <= float(load.budget_per_kg) * 1.10:
                score += 10
                
        # KYC verified owner
        if owner.kyc_status == KycStatus.verified:
            score += 20
            
        # Create or update Match record
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
            
        # Flush to ensure match.id is generated
        db.flush()
        
        match_results.append({
            "match_id": str(match.id),
            "score": float(score),
            "from_city": listing.from_city,
            "to_city": listing.to_city,
            "departure_date": listing.departure_date.isoformat(),
            "available_capacity_kg": listing.available_capacity_kg,
            "price_per_kg": float(listing.price_per_kg),
            "owner_name": owner.name,
            "owner_phone": owner.phone
        })
        
    db.commit()
    
    # Sort by score descending and return top 5
    match_results.sort(key=lambda x: x["score"], reverse=True)
    return match_results[:5]

def update_listing_capacity_after_match(db: Session, match_id: str):
    match = db.query(Match).filter(Match.id == match_id).first()
    if not match:
        raise ValueError("Match not found")
        
    listing = db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match.listing_id).first()
    load = db.query(LoadRequest).filter(LoadRequest.id == match.load_request_id).first()
    
    if not listing or not load:
        raise ValueError("Listing or LoadRequest not found")
        
    if listing.available_capacity_kg < load.weight_kg:
         raise ValueError("Not enough capacity in the listing")
         
    listing.available_capacity_kg -= load.weight_kg
    
    if listing.available_capacity_kg <= 0:
        listing.status = ListingStatus.full
    else:
        listing.status = ListingStatus.partial
        
    load.status = LoadRequestStatus.confirmed
    match.status = MatchStatus.accepted
    
    db.commit()
