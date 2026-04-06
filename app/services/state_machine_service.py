import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass

from app.contracts.enums import Intent

logger = logging.getLogger(__name__)

@dataclass
class TransitionResult:
    allowed: bool
    next_state: str
    side_effect: Optional[str] = None
    error_message: Optional[str] = None

class StateMachineService:
    ACTIVE_WORKFLOWS = frozenset({
        "LOAD_FLOW",
        "TRUCK_FLOW",
    })
    TERMINAL_WORKFLOWS = frozenset({
        "SUCCESS",
        "CANCELLED",
        "FAILED",
    })
    ROUTING_AUTHORITY_FIELDS = (
        "lane_key",
        "directional_lane_key",
        "from_city",
        "to_city",
    )
    PROVENANCE_FIELDS = (
        "lane_key",
        "directional_lane_key",
        "confidence_source",
        "corridor_source",
        "resolver_version",
    )

    # Hierarchical Transition Matrix
    # Format: {CURRENT_STATE: {INTENT: NEXT_STATE}}
    TRANSITIONS = {
        "IDLE": {
            Intent.CREATE_LOAD: "LOAD_FLOW",
            Intent.POST_TRUCK: "TRUCK_FLOW",
            Intent.VIEW_LOADS: "IDLE",
            Intent.VIEW_TRUCKS: "IDLE",
            Intent.UPLOAD_KYC: "IDLE",
            Intent.RATE_TRIP: "IDLE",
            Intent.TRACK_TRUCK: "IDLE",
            Intent.CONTACT_DRIVER: "IDLE",
            Intent.CONFIRM_BOOKING: "IDLE",
            Intent.UNKNOWN: "IDLE",
        },
        "LOAD_FLOW": {
            Intent.CONFIRM: "IDLE",
            Intent.CANCEL: "IDLE",
            Intent.CREATE_LOAD: "LOAD_FLOW", # Re-trigger or update
            Intent.UNKNOWN: "LOAD_FLOW",     # Stay and clarify
            Intent.VIEW_LOADS: "IDLE",
            Intent.UPLOAD_KYC: "IDLE",
            Intent.RATE_TRIP: "IDLE",
            Intent.TRACK_TRUCK: "IDLE",
            Intent.CONTACT_DRIVER: "IDLE",
            Intent.CONFIRM_BOOKING: "IDLE",
        },
        "TRUCK_FLOW": {
            Intent.CONFIRM: "IDLE",
            Intent.CANCEL: "IDLE",
            Intent.POST_TRUCK: "TRUCK_FLOW",
            Intent.UNKNOWN: "TRUCK_FLOW",    # Stay and clarify
            Intent.VIEW_TRUCKS: "IDLE",
            Intent.UPLOAD_KYC: "IDLE",
            Intent.RATE_TRIP: "IDLE",
            Intent.TRACK_TRUCK: "IDLE",
            Intent.CONTACT_DRIVER: "IDLE",
            Intent.CONFIRM_BOOKING: "IDLE",
        }
    }

    STATE_MIGRATIONS = {
        "legacy_collecting": "LOAD_FLOW",
        "pending_confirmation": "LOAD_FLOW",
    }

    SESSION_TTL = timedelta(minutes=30)

    @classmethod
    def workflow_is_active(cls, workflow: Optional[str]) -> bool:
        if not workflow:
            return False
        state = cls.STATE_MIGRATIONS.get(workflow, workflow)
        return state in cls.ACTIVE_WORKFLOWS or str(state).endswith("_CONFIRM")

    @classmethod
    def workflow_is_confirm_stage(cls, workflow: Optional[str]) -> bool:
        if not workflow:
            return False
        state = cls.STATE_MIGRATIONS.get(workflow, workflow)
        return str(state).endswith("_CONFIRM")

    @classmethod
    def workflow_is_terminal(cls, workflow: Optional[str]) -> bool:
        if not workflow:
            return False
        state = cls.STATE_MIGRATIONS.get(workflow, workflow)
        return state in cls.TERMINAL_WORKFLOWS

    @classmethod
    def missing_routing_authority_fields(cls, session_data: Optional[dict]) -> list[str]:
        data = session_data if isinstance(session_data, dict) else {}
        return [field for field in cls.ROUTING_AUTHORITY_FIELDS if not data.get(field)]

    @classmethod
    def reconstruct_workflow(cls, workflow: Optional[str], session_data: Optional[dict]) -> Optional[str]:
        if not workflow:
            return None

        state = cls.STATE_MIGRATIONS.get(workflow, workflow)

        if cls.workflow_is_terminal(state):
            logger.warning(
                "[STALE_WORKFLOW_IGNORED]",
                extra={"workflow": state},
            )
            return None

        if cls.workflow_is_active(state):
            missing = cls.missing_routing_authority_fields(session_data)
            if missing:
                logger.warning(
                    "[SESSION_RECONSTRUCTION_ABORT]",
                    extra={
                        "workflow": state,
                        "missing": missing,
                    },
                )
                return None

        return state

    @classmethod
    def cleanup_terminal_state(cls, session_data: Optional[dict]) -> dict:
        data = session_data if isinstance(session_data, dict) else {}
        for field in cls.PROVENANCE_FIELDS:
            if data.get(field):
                logger.warning(
                    "[TERMINAL_METADATA_LEAK]",
                    extra={"field": field},
                )
        data.clear()
        return data

    @staticmethod
    def _as_utc(last_updated: Optional[datetime]) -> Optional[datetime]:
        if last_updated is None:
            return None
        if last_updated.tzinfo is None:
            return last_updated.replace(tzinfo=timezone.utc)
        return last_updated.astimezone(timezone.utc)

    def transition(self, current_state: str, intent: Intent, last_updated: Optional[datetime]) -> TransitionResult:
        """
        Strict Transition Matrix Check with HSM and TTL support.
        """
        # 1. State Migration
        state = self.STATE_MIGRATIONS.get(current_state, current_state)

        # 2. TTL Check (Auto-Reset)
        normalized_last_updated = self._as_utc(last_updated)
        if normalized_last_updated and datetime.now(timezone.utc) - normalized_last_updated > self.SESSION_TTL:
            logger.info(f"Session expired (State: {state}). Reverting to IDLE.")
            state = "IDLE"

        # 3. Transition Check
        allowed_intents = self.TRANSITIONS.get(state, {})
        
        if intent in allowed_intents:
            next_state = allowed_intents[intent]
            side_effect = self._get_side_effect(state, next_state, intent)
            return TransitionResult(allowed=True, next_state=next_state, side_effect=side_effect)

        # Special case: Global intents (Cancel, Main Menu)
        if intent == Intent.CANCEL:
            return TransitionResult(allowed=True, next_state="IDLE", side_effect="CLEAR_SESSION")

        return TransitionResult(
            allowed=False, 
            next_state=state, 
            error_message="⚠️ This action is not allowed in your current state."
        )

    def _get_side_effect(self, from_state: str, to_state: str, intent: Intent) -> Optional[str]:
        if from_state == "IDLE" and to_state == "LOAD_FLOW":
            return "INIT_LOAD_SESSION"
        if from_state == "IDLE" and to_state == "TRUCK_FLOW":
            return "INIT_TRUCK_SESSION"
        if intent == Intent.CONFIRM:
            return "COMMIT_DATA"
        return None
