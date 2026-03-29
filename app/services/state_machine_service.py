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
