from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EventLog
from app.services.debug_logger import DebugLogger

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
    
    metric_logs = db.query(EventLog).filter(EventLog.event_type == "DEBUG_AI_METRIC").all()
    total_ai = len(metric_logs)
    success_ai = sum(1 for log in metric_logs if (log.data or {}).get("val_status") == "action_extracted")
    
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
                "data": log.data or {},
                "payload": log.data or {},
            } for log in logs
        ]
    }
