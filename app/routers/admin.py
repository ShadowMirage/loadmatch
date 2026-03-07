from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel

from app.database import get_db
from app.config import settings
from app.models.user import User, KycStatus, UserRole
from app.models.kyc import KycDocument, DocType
from app.models.match import Match, MatchStatus
from app.models.listing import TruckSpaceListing, ListingStatus
from app.models.load_request import LoadRequest, LoadRequestStatus

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
    user.kyc_status = KycStatus.verified
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
    user.kyc_status = KycStatus.rejected
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

@router.get("/stats")
def get_stats(db: Session = Depends(get_db), admin_key: str = Depends(verify_admin_key)):
    # User Stats
    total_users = db.query(User).count()
    verified_users = db.query(User).filter(User.kyc_status == KycStatus.verified).count()
    pending_kyc_users = db.query(User).filter(User.kyc_status == KycStatus.pending).count()
    
    # Platform Stats
    total_load_requests = db.query(LoadRequest).filter(
        LoadRequest.status == LoadRequestStatus.open
    ).count()
    
    total_listings = db.query(TruckSpaceListing).filter(
        TruckSpaceListing.status == ListingStatus.open
    ).count()
    
    # Matches Stats
    total_matches_suggested = db.query(Match).filter(Match.status == MatchStatus.suggested).count()
    total_matches_accepted = db.query(Match).filter(Match.status == MatchStatus.accepted).count()
    
    return {
        "total_users": total_users,
        "verified_users": verified_users,
        "pending_kyc_users": pending_kyc_users,
        "total_load_requests": total_load_requests,
        "total_listings": total_listings,
        "total_matches_suggested": total_matches_suggested,
        "total_matches_accepted": total_matches_accepted
    }
