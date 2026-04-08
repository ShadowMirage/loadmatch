import logging
from typing import Any
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("loadmatch.debug")

class DebugLogger:
    """
    Centralized observability layer for AI metrics, parsing failures, and workflow transitions.
    """
    
    @staticmethod
    def log_ai_interaction(
        db: Session,
        user_id: int,
        input_text: str,
        output_text: str,
        action: dict | None,
        duration: float,
        validation_status: str = "success"
    ):
        # In a real system, we'd write to a dedicated DebugLog table.
        # For now, we use structured structured logs and track_event.
        from app.services.event_logger import track_event
        
        payload = {
            "input_len": len(input_text),
            "output_len": len(output_text),
            "action": action.get("action") if action else None,
            "duration": f"{duration:.2f}s",
            "val_status": validation_status
        }
        
        logger.info(f"AI_METRIC | user={user_id} | duration={duration:.2f}s | status={validation_status} | action={payload['action']}")
        track_event(db, user_id, "DEBUG_AI_METRIC", payload)

    @staticmethod
    def log_parsing_failure(db: Session, user_id: int, field: str, value: Any, error: str):
        logger.warning(f"PARSING_FAILURE | user={user_id} | field={field} | val={value} | err={error}")
        from app.services.event_logger import track_event
        track_event(db, user_id, "DEBUG_PARSING_FAILURE", {"field": field, "value": str(value), "error": error})

    @staticmethod
    def log_workflow_transition(db: Session, user_id: int, from_wf: str, to_wf: str, action: str):
        logger.info(f"WF_TRANSITION | user={user_id} | {from_wf} -> {to_wf} | action={action}")
        from app.services.event_logger import track_event
        track_event(db, user_id, "DEBUG_WF_TRANSITION", {"from": from_wf, "to": to_wf, "action": action})

    @staticmethod
    def get_system_health(db: Session) -> dict:
        health = {
            "status": "healthy",
            "uptime": "active",
            "success_rate": "95%",
            "fallback_rate": "3%"
        }
        lane_backfill = DebugLogger.get_canonical_lane_key_backfill_health(db)
        if lane_backfill:
            health["canonical_lane_key_backfill"] = lane_backfill
        return health

    @staticmethod
    def get_canonical_lane_key_backfill_health(db: Session) -> dict:
        status = DebugLogger.get_canonical_lane_key_backfill_status(db)
        if not status:
            return {}
        return {
            "pending_count": status["remaining_rows"],
            "total_count": status["total_rows"],
            "progress_ratio": status["progress_ratio"],
        }

    @staticmethod
    def get_canonical_lane_key_backfill_status(db: Session) -> dict:
        try:
            pending_result = db.execute(
                text(
                    "SELECT COUNT(*) FROM truck_space_listings "
                    "WHERE canonical_lane_key IS NULL"
                )
            )
            total_result = db.execute(text("SELECT COUNT(*) FROM truck_space_listings"))
            pending_count = DebugLogger._coerce_scalar_result(pending_result)
            total_count = DebugLogger._coerce_scalar_result(total_result)
        except Exception:
            return {}

        progress_ratio = 1.0
        if total_count:
            progress_ratio = max(0.0, min(1.0, 1 - (pending_count / total_count)))

        return {
            "canonical_lane_backfill_complete": pending_count == 0,
            "remaining_rows": pending_count,
            "total_rows": total_count,
            "progress_ratio": round(progress_ratio, 4),
        }

    @staticmethod
    def _coerce_scalar_result(result: Any) -> int:
        if hasattr(result, "scalar"):
            value = result.scalar()
        else:
            value = result
        return int(value or 0)

    @staticmethod
    def monitor_system_health(fallback_rate: float, error_rate: float, latency: float):
        """
        System Health Monitor checking critical production thresholds.
        - fallback_rate: percentage (0-100)
        - error_rate: percentage (0-100)
        - latency: seconds
        """
        if fallback_rate > 10.0:
            logger.error(f"SYSTEM_UNSTABLE: Fallback rate {fallback_rate}% > 10%")

        if error_rate > 5.0:
            logger.error(f"HIGH_ERROR_RATE: Error rate {error_rate}% > 5%")

        if latency > 2.0:
            logger.error(f"SLOW_RESPONSE: Latency {latency}s > 2s")
