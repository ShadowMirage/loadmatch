import datetime
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel

from app.database import get_db
from app.config import settings
from app.models.enums import UserRole, KycFlowState, DocType, MatchStatus, ListingStatus, LoadRequestStatus
from app.models.user import User
from app.models.kyc import KycDocument
from app.models.match import Match
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.truck import Truck
from app.models.event import EventLog

router = APIRouter(prefix="/admin", tags=["Admin"])

def verify_admin_key(x_admin_key: str = Header(...)):
    if x_admin_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin API key")
    return x_admin_key

@router.get("/kyc/pending")
def get_pending_kyc(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    documents = (
        db.query(KycDocument, User)
        .join(User, KycDocument.user_id == User.id)
        .filter(KycDocument.verified == False, KycDocument.rejection_reason == None)
        .order_by(KycDocument.uploaded_at.asc())
        .all()
    )
    
    results = []
    for doc, user in documents:
        results.append({
            "doc_id": str(doc.id),
            "user_phone": user.phone,
            "user_name": user.name,
            "user_role": user.role.value if user.role else None,
            "doc_type": doc.doc_type.value,
            "file_url": doc.file_url,
            "uploaded_at": doc.uploaded_at.isoformat()
        })
        
    return results

@router.post("/kyc/{doc_id}/verify")
def verify_kyc_document(doc_id: str, db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    doc = db.query(KycDocument).filter(KycDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    user = db.query(User).filter(User.id == doc.user_id).first()
    
    doc.verified = True
    
    # Check if user has at least one verified document
    # (Since we just set this one to True, it should be at least 1, but we can just set it)
    user.kyc_flow_state = KycFlowState.verified
    db.commit()
        
    return {
        "status": "success", 
        "message": f"Document verified successfully",
        "user_phone": user.phone
    }

class RejectReason(BaseModel):
    reason: str

@router.post("/kyc/{doc_id}/reject")
def reject_kyc_document(doc_id: str, payload: RejectReason, db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    doc = db.query(KycDocument).filter(KycDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    user = db.query(User).filter(User.id == doc.user_id).first()
    
    doc.rejection_reason = payload.reason
    user.kyc_flow_state = KycFlowState.rejected
    db.commit()
    
    return {
        "status": "success",
        "message": f"Document rejected for {user.phone}"
    }

@router.get("/matches/recent")
def get_recent_matches(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    matches = (
        db.query(Match, TruckSpaceListing, LoadRequest, User.phone.label("shipper_phone"))
        .join(TruckSpaceListing, Match.listing_id == TruckSpaceListing.id)
        .join(LoadRequest, Match.load_request_id == LoadRequest.id)
        .join(User, LoadRequest.shipper_id == User.id)
        .filter(Match.status.in_([MatchStatus.accepted, MatchStatus.suggested]))
        .order_by(Match.matched_at.desc())
        .limit(20)
        .all()
    )
    
    # For transporter phone we need to query user separately for listing owner
    results = []
    for match, listing, load, shipper_phone in matches:
        transporter = db.query(User).filter(User.id == listing.owner_id).first()
        
        results.append({
            "match_id": str(match.id),
            "status": match.status.value,
            "from_city": load.from_city,
            "to_city": load.to_city,
            "load_weight_kg": load.weight_kg,
            "price_per_kg": float(listing.price_per_kg),
            "shipper_phone": shipper_phone,
            "transporter_phone": transporter.phone if transporter else None,
            "match_score": float(match.match_score),
            "matched_at": match.matched_at.isoformat()
        })
        
    return results

@router.get("/active-loads")
def get_active_loads(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    loads = db.query(LoadRequest, User).join(User, LoadRequest.shipper_id == User.id).filter(
        LoadRequest.status == LoadRequestStatus.open
    ).order_by(LoadRequest.created_at.desc()).all()
    
    return [
        {
            "id": str(load.id),
            "shipper_phone": user.phone,
            "route": f"{load.from_city} -> {load.to_city}",
            "weight_kg": load.weight_kg,
            "category": load.category.value if load.category else None,
            "pickup_date": load.pickup_date.isoformat(),
            "created_at": load.created_at.isoformat()
        } for load, user in loads
    ]

@router.get("/active-trucks")
def get_active_trucks(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    trucks = db.query(TruckSpaceListing, User, Truck).join(
        User, TruckSpaceListing.owner_id == User.id
    ).join(
        Truck, TruckSpaceListing.truck_id == Truck.id
    ).filter(
        TruckSpaceListing.status == ListingStatus.open
    ).order_by(TruckSpaceListing.created_at.desc()).all()
    
    return [
        {
            "id": str(listing.id),
            "transporter_phone": user.phone,
            "truck_number": truck.registration_number,
            "route": f"{listing.from_city} -> {listing.to_city}",
            "available_capacity_kg": listing.available_capacity_kg,
            "price_per_kg": float(listing.price_per_kg),
            "departure_date": listing.departure_date.isoformat(),
            "created_at": listing.created_at.isoformat()
        } for listing, user, truck in trucks
    ]

@router.get("/matches")
def get_all_matches(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    matches = db.query(Match, LoadRequest, TruckSpaceListing).join(
        LoadRequest, Match.load_request_id == LoadRequest.id
    ).join(
        TruckSpaceListing, Match.listing_id == TruckSpaceListing.id
    ).order_by(Match.matched_at.desc()).limit(50).all()
    
    return [
        {
            "id": str(m.id),
            "status": m.status.value,
            "route": f"{l.from_city} -> {l.to_city}",
            "weight_kg": l.weight_kg,
            "price_per_kg": float(t.price_per_kg),
            "matched_at": m.matched_at.isoformat()
        } for m, l, t in matches
    ]

@router.get("/stats")
def get_stats(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    # User Stats
    users = db.query(User).count()
    verified_users = db.query(User).filter(User.kyc_flow_state == KycFlowState.verified).count()
    pending_kyc = db.query(User).filter(User.kyc_flow_state == KycFlowState.under_review).count()
    
    # Platform Stats
    loads = db.query(LoadRequest).count()
    trucks = db.query(TruckSpaceListing).count()
    matches = db.query(Match).count()
    
    active_matches = db.query(Match).filter(Match.status.in_([MatchStatus.pending, MatchStatus.accepted, MatchStatus.confirmed, MatchStatus.suggested])).count()
    
    # Today's Stats
    today = datetime.datetime.now().date()
    today_loads = db.query(LoadRequest).filter(func.date(LoadRequest.created_at) == today).count()
    today_trucks = db.query(TruckSpaceListing).filter(func.date(TruckSpaceListing.created_at) == today).count()
    today_matches = db.query(Match).filter(func.date(Match.matched_at) == today).count()
    
    return {
        "users": users,
        "verified_users": verified_users,
        "pending_kyc": pending_kyc,
        "loads": loads,
        "trucks": trucks,
        "matches": matches,
        "active_matches": active_matches,
        "today_loads": today_loads,
        "today_trucks": today_trucks,
        "today_matches": today_matches
    }

@router.get("/events")
def get_recent_events(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key), limit: int = 50):
    events = (
        db.query(EventLog, User)
        .outerjoin(User, EventLog.user_id == User.id)
        .order_by(EventLog.created_at.desc())
        .limit(limit)
        .all()
    )
    
    return [
        {
            "id": str(evt.id),
            "event_type": evt.event_type,
            "user_phone": user.phone if user else "System",
            "data": evt.data,
            "created_at": evt.created_at.isoformat()
        } for evt, user in events
    ]
