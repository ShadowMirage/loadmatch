import logging
import dataclasses
from typing import Any, Dict
from app.contracts.enums import Intent
from app.contracts.payloads import CreateLoadPayload, PostTruckPayload, GenericActionPayload

logger = logging.getLogger(__name__)

class PayloadFactory:
    """
    Strictly builds and validates payloads. 
    No silent corrections are allowed past this point.
    """
    
    PAYLOAD_MAP: Dict[Intent, Any] = {
        Intent.CREATE_LOAD: CreateLoadPayload,
        Intent.POST_TRUCK: PostTruckPayload,
        # Intent.CONFIRM: GenericActionPayload,
        # Intent.CANCEL: GenericActionPayload,
    }

    @staticmethod
    def serialize(payload: Any) -> Any:
        if dataclasses.is_dataclass(payload):
            serialized = dataclasses.asdict(payload)
            extraction_data = getattr(payload, "extraction_data", None)
            if isinstance(extraction_data, dict):
                serialized["extraction_data"] = extraction_data
            return serialized
        return payload

    @staticmethod
    def extract_replay_data(request_payload: Dict[str, Any] | None) -> Dict[str, Any]:
        request_payload = request_payload or {}

        extraction_data = request_payload.get("extraction_data")
        if isinstance(extraction_data, dict):
            return extraction_data

        payload_data = request_payload.get("payload")
        if isinstance(payload_data, dict):
            action_data = payload_data.get("data")
            if isinstance(action_data, dict):
                return action_data
            return payload_data

        return {}

    def build(self, intent: Intent, data: Dict[str, Any]) -> Any:
        normalized_data = self._normalize_aliases(intent, data or {})
        payload_class = self.PAYLOAD_MAP.get(intent)
        
        if not payload_class:
            # Fallback for intents that don't need structured payloads
            return GenericActionPayload(action=intent.value, data=normalized_data)

        # 🛡️ FAIL-FAST SCHEMA GUARD
        REQUIRED_FIELDS = {
            Intent.CREATE_LOAD: ["from_city", "to_city"],
            Intent.POST_TRUCK: ["current_city"],
        }
        missing_fields = [
            field
            for field in REQUIRED_FIELDS.get(intent, [])
            if normalized_data.get(field) is None
        ]
        if missing_fields:
            # Allow the dispatcher to continue slot collection instead of collapsing back
            # to the main menu on partial user input.
            return GenericActionPayload(action=intent.value, data=normalized_data)

        try:
            # 🔒 STRICT FILTERING: Only pass fields that exist in the dataclass
            valid_fields = {f.name for f in dataclasses.fields(payload_class)}
            filtered_data = {k: v for k, v in normalized_data.items() if k in valid_fields}

            # This triggers __post_init__ validation automatically
            payload = payload_class(**filtered_data)
            # Preserve replay/corridor provenance on typed payloads so dispatcher
            # pacing and replay equivalence do not depend on GenericActionPayload.
            setattr(payload, "extraction_data", dict(normalized_data))
            return payload
        except TypeError as e:
            logger.info(f"Payload still partial for {intent}: {e}")
            return GenericActionPayload(action=intent.value, data=normalized_data)
        except Exception as e:
            from app.core.exceptions import CriticalLogicError
            logger.error(f"Payload validation failed for {intent}: {e}")
            raise CriticalLogicError(f"Schema validation failed: {str(e)}")

    @staticmethod
    def _normalize_aliases(intent: Intent, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(data)

        if intent == Intent.CREATE_LOAD:
            if "cargo" in normalized and "material_type" not in normalized:
                normalized["material_type"] = normalized["cargo"]
            if "date" in normalized and "pickup_date" not in normalized:
                normalized["pickup_date"] = normalized["date"]

        if intent == Intent.POST_TRUCK:
            if "from_city" in normalized and "current_city" not in normalized:
                normalized["current_city"] = normalized["from_city"]
            if "weight_kg" in normalized and "capacity_kg" not in normalized:
                normalized["capacity_kg"] = normalized["weight_kg"]
            if "date" in normalized and "departure_date" not in normalized:
                normalized["departure_date"] = normalized["date"]

        return normalized
