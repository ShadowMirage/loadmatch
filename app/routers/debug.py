from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import EventLog
from app.services.debug_logger import DebugLogger
import json

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/dashboard")
def debug_dashboard(db: Session = Depends(get_db)):
    """
    Lightweight observability dashboard (Phase 2).
    Shows recent AI logs and system health metrics.
    """
    # Fetch last 50 debug events
    logs = db.query(EventLog).filter(
        EventLog.event_type.like("DEBUG_%")
    ).order_by(EventLog.created_at.desc()).limit(50).all()
    
    # Calculate health metrics
    total_ai = db.query(EventLog).filter(EventLog.event_type == "DEBUG_AI_METRIC").count()
    success_ai = db.query(EventLog).filter(
        EventLog.event_type == "DEBUG_AI_METRIC",
        EventLog.payload["val_status"].astext == "action_extracted"
    ).count()
    
    health = DebugLogger.get_system_health(db)
    if total_ai > 0:
        health["success_rate"] = f"{(success_ai / total_ai) * 100:.1f}%"
    
    return {
        "system_health": health,
        "recent_logs": [
            {
                "time": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "type": log.event_type,
                "user": log.user_id,
                "payload": log.payload
            } for log in logs
        ]
    }
