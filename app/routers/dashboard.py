from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import settings
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.routers import admin

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="app/admin_dashboard/templates")

@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    admin_key = settings.ADMIN_API_KEY
    
    stats = admin.get_stats(db, admin_key)
    loads = admin.get_active_loads(db, admin_key)
    trucks = admin.get_active_trucks(db, admin_key)
    matches = admin.get_all_matches(db, admin_key)
    pending_kyc = admin.get_pending_kyc(db, admin_key)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "loads": loads,
        "trucks": trucks,
        "matches": matches,
        "pending_kyc": pending_kyc
    })

@router.get("/dashboard/events")
def dashboard_events(request: Request, db: Session = Depends(get_db)):
    admin_key = settings.ADMIN_API_KEY
    events = admin.get_recent_events(db, admin_key, limit=100)
    
    return templates.TemplateResponse("events.html", {
        "request": request,
        "events": events
    })

@router.get("/loads")
def get_loads(db: Session = Depends(get_db)):
    return db.query(LoadRequest).all()

@router.get("/trucks")
def get_trucks(db: Session = Depends(get_db)):
    return db.query(TruckSpaceListing).all()
