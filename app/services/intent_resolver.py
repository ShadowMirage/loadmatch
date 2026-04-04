import logging
from typing import Optional, Dict, Any
from app.contracts.enums import Intent
from app.contracts.extraction import ExtractionResult
from app.services.logistics_data import (
    CITY_LOGISTICS_HUBS, CITY_ALIASES, ROUTE_CONNECTORS,
    normalize_hub_name, CorridorSource, get_corridor_source,
    get_lane_class, RESOLVER_VERSION
)
from app.services.meta_intents import is_greeting

logger = logging.getLogger(__name__)

# Stage 4: Confidence Routing Threshold
CONFIDENCE_THRESHOLD = 0.6

class IntentResolver:
    def resolve(
        self, 
        extraction: ExtractionResult, 
        interactive_payload: Optional[Dict[str, Any]], 
        current_workflow: Optional[str],
        message_text: Optional[str] = ""
    ) -> Intent:
        """
        3-Input Deterministic Resolver with Stage 4 Confidence Routing.
        
        Order of priority: 
        1. Interactive Payload (Buttons/Lists) — always high confidence
        2. Extraction Result (gated by confidence threshold)
        3. Session Workflow (Context fallback)
        
        Stage 4 Rule: if confidence < 0.6, route to UNKNOWN
        which triggers confirm_with_user() in the orchestrator.
        This does NOT trigger dispatcher execution.
        """
        
        # 1. Interactive Payload (Direct User Action — always trusted)
        if interactive_payload:
            action_id = interactive_payload.get("id", "").upper()
            if action_id.startswith("RATING_"):
                return Intent.RATE_TRIP
            if action_id.startswith("TRACK_TRUCK_"):
                return Intent.TRACK_TRUCK
            if action_id.startswith("CONTACT_DRIVER_"):
                return Intent.CONTACT_DRIVER
            if action_id.startswith("CONFIRM_BOOKING_"):
                return Intent.CONFIRM_BOOKING
            if action_id.startswith("CANCEL_BOOKING_"):
                return Intent.CANCEL
            if action_id in ("CONFIRM_LOAD", "CONFIRM_TRUCK", "CONFIRM_LOAD_REQUEST", "CONFIRM_TRUCK_LISTING"):
                return Intent.CONFIRM
            if action_id in ("CANCEL_LOAD", "CANCEL_TRUCK", "CANCEL", "MAIN_MENU"):
                return Intent.CANCEL
            if action_id in ("POST_TRUCK", "START_TRUCK"):
                return Intent.POST_TRUCK
            if action_id in ("FIND_TRUCK", "POST_LOAD"):
                return Intent.CREATE_LOAD
            if action_id == "TRACK_BOOKING":
                return Intent.VIEW_LOADS
            if action_id == "DELIVERY_STATUS":
                return Intent.VIEW_TRUCKS
            if action_id == "UPLOAD_KYC":
                return Intent.UPLOAD_KYC
            if action_id == "VIEW_LOADS":
                return Intent.VIEW_LOADS
            if action_id == "VIEW_TRUCKS":
                return Intent.VIEW_TRUCKS
            # ... add more mappings as needed

        if extraction.intent == Intent.GREETING:
            return Intent.GREETING

        # 2. Extraction Result (Confidence-gated)
        if extraction.confidence >= CONFIDENCE_THRESHOLD:
            if extraction.intent != Intent.UNKNOWN:
                logger.debug(
                    f"[INTENT_RESOLVE] Accepted: {extraction.intent.value} "
                    f"(confidence={extraction.confidence:.2f})"
                )
                return extraction.intent
        
        # 3. Heuristic Intent (Corridor & Keyword detection)
        # This layer can push confidence to 1.0 and override LLM UNKNOWN
        heuristic = self._heuristic_resolve_text(
            message_text,
            extraction,
            current_workflow,
            RESOLVER_VERSION,
        )
        if heuristic:
            return heuristic

        # 4. Session Context Fallback
        if current_workflow == "LOAD_FLOW" and extraction.data:
            return Intent.CREATE_LOAD
        if current_workflow == "LOAD_FLOW":
            return Intent.UNKNOWN

        if current_workflow == "TRUCK_FLOW" and extraction.data:
            return Intent.POST_TRUCK

        heuristic_intent = self._infer_from_entities(extraction.data or {})
        if heuristic_intent:
            return heuristic_intent

        # 2. Greeting / Menu Hijack (Interrupt intents)
        if is_greeting(message_text):
            extraction.confidence = 1.0
            extraction.data = {}
            extraction.data["resolver_version"] = RESOLVER_VERSION
            return Intent.GREETING

        return Intent.UNKNOWN

    # is_greeting moved to app/services/meta_intents.py

    @staticmethod
    def _corridor_intent_for_workflow(current_workflow: Optional[str]) -> Intent:
        if current_workflow == "TRUCK_FLOW":
            return Intent.POST_TRUCK
        return Intent.CREATE_LOAD

    @staticmethod
    def _explicit_keyword_intent(lower_text: str, token_set: set[str]) -> Optional[Intent]:
        load_phrases = (
            "post load",
            "need load",
            "have a load",
            "have load",
            "i have load",
            "i have a load",
        )
        truck_phrases = (
            "post truck",
            "need truck",
            "have a truck",
            "have truck",
            "truck available",
            "vehicle available",
            "empty truck",
            "truck empty",
        )

        if any(phrase in lower_text for phrase in load_phrases):
            return Intent.CREATE_LOAD
        if any(phrase in lower_text for phrase in truck_phrases):
            return Intent.POST_TRUCK

        load_tokens = {"load"}
        truck_tokens = {"truck", "vehicle", "lorry", "tempo", "lorries", "gaadi"}
        has_load = bool(token_set & load_tokens)
        has_truck = bool(token_set & truck_tokens)

        if has_load and not has_truck:
            return Intent.CREATE_LOAD
        if has_truck and not has_load:
            return Intent.POST_TRUCK
        return None

    @classmethod
    def _apply_corridor_metadata(
        cls,
        extraction: ExtractionResult,
        *,
        city_from: str,
        city_to: str,
        corridor_source: CorridorSource,
        lane_detected_via: str,
        version: str,
        current_workflow: Optional[str],
        explicit_intent: Optional[Intent],
    ) -> Intent:
        extraction.data.update({
            "from_city": city_from,
            "to_city": city_to,
            "lane_cities": [city_from, city_to],
            "lane_key": ":".join(sorted([city_from, city_to])),
            "directional_lane_key": f"{city_from}->{city_to}",
            "reverse_directional_lane_key": f"{city_to}->{city_from}",
            "lane_class": get_lane_class(city_from, city_to),
            "corridor_detected": True,
            "corridor_source": corridor_source,
            "confidence_source": "corridor_detection",
            "lane_detected_via": lane_detected_via,
            "resolver_version": version,
        })
        extraction.confidence = 1.0
        return explicit_intent or cls._corridor_intent_for_workflow(current_workflow)

    @classmethod
    def _heuristic_resolve_text(
        cls,
        text: Optional[str],
        extraction: ExtractionResult,
        current_workflow: Optional[str],
        version: str,
    ) -> Optional[Intent]:
        if not text:
            return None
        
        # Normalize separators for window-based detection (handle 'delhi-jaipur' as 'delhi - jaipur')
        lower_text = text.lower().strip()
        processed_text = lower_text.replace("-", " - ").replace("/", " / ")
        tokens = processed_text.split()
        token_set = set(tokens)
        explicit_intent = cls._explicit_keyword_intent(lower_text, token_set)

        # Helper to check for city entities in a window
        def check_multiword_at(tokens_list, index) -> Optional[tuple[str, str]]:
            """Returns (canonical_name, raw_matched_string) if found."""
            if index < 0 or index >= len(tokens_list):
                return None
            
            # 1. Double word check forward (e.g. index="ankleshwar", index+1="gidc")
            if index < len(tokens_list) - 1:
                combined = f"{tokens_list[index]} {tokens_list[index+1]}"
                res = normalize_hub_name(combined)
                if res:
                    return res, combined
            
            # 2. Double word check backward (e.g. index="gidc", index-1="ankleshwar")
            if index > 0:
                combined = f"{tokens_list[index-1]} {tokens_list[index]}"
                res = normalize_hub_name(combined)
                if res:
                    return res, combined
            
            # 3. Single word check
            single = normalize_hub_name(tokens_list[index])
            if single:
                return single, tokens_list[index]
            
            return None

        # 1. Strict Window-Based Corridor Detection
        for i, token in enumerate(tokens):
            if token in ROUTE_CONNECTORS:
                res_from = check_multiword_at(tokens, i-1)
                res_to = check_multiword_at(tokens, i+1)
                
                if res_from and res_to:
                    city_from, raw_from = res_from
                    city_to, raw_to = res_to
                    
                    logger.info(f"[CORRIDOR_DETECT] Found route: {city_from} -> {city_to}. Boosting confidence to 1.0.")
                    return cls._apply_corridor_metadata(
                        extraction,
                        city_from=city_from,
                        city_to=city_to,
                        corridor_source=get_corridor_source(raw_from, raw_to),
                        lane_detected_via="separator_window",
                        version=version,
                        current_workflow=current_workflow,
                        explicit_intent=explicit_intent,
                    )

        # 2. Adjacency Shorthand (delhi jaipur, delhi/jaipur, jaipur delhi route)
        # Find all recognized cities in the message
        found_cities = []
        skip_next = False
        for i, t in enumerate(tokens):
            if skip_next:
                skip_next = False
                continue
            
            # Check for multi-word city first
            if i < len(tokens) - 1:
                combined = f"{t} {tokens[i+1]}"
                res = CITY_ALIASES.get(combined) or (combined if combined in CITY_LOGISTICS_HUBS else None)
                if res:
                    found_cities.append(res)
                    skip_next = True
                    continue
            
            # Single word city
            res = normalize_hub_name(t)
            if res:
                found_cities.append(res)

        if len(found_cities) >= 2:
            # delhi-jaipur, delhi jaipur route, etc.
            city_from, city_to = found_cities[0], found_cities[1]
            logger.info(f"[ADJACENCY_DETECT] Found city pair: {city_from} {city_to}. Payload enriched.")
            return cls._apply_corridor_metadata(
                extraction,
                city_from=city_from,
                city_to=city_to,
                corridor_source=CorridorSource.ADJACENT_CITY_PAIR,
                lane_detected_via="adjacent_tokens",
                version=version,
                current_workflow=current_workflow,
                explicit_intent=explicit_intent,
            )

        if explicit_intent:
            extraction.confidence = min(extraction.confidence + 0.25, 0.92)
            return explicit_intent

        # 3. Token-Cluster Signals (Marketplace priority)
        LOAD_SIGNALS = {
            "load", "shipment", "cargo", "goods", "parcel", "consignment", 
            "booking", "maal", "samaan", "material", "freight"
        }
        TRUCK_SIGNAL_TOKENS = {
            "truck", "vehicle", "lorry", "tempo", "available", "container", 
            "lorries", "gaadi", "empty", "space", "placement"
        }

        if token_set & LOAD_SIGNALS:
            extraction.confidence = min(extraction.confidence + 0.25, 0.92)
            return Intent.CREATE_LOAD
        
        if token_set & TRUCK_SIGNAL_TOKENS:
            extraction.confidence = min(extraction.confidence + 0.25, 0.92)
            return Intent.POST_TRUCK
            
        return None

    @staticmethod
    def _infer_from_entities(data: Dict[str, Any]) -> Optional[Intent]:
        if not data:
            return None

        truck_signals = {"capacity_kg", "plate", "current_city", "rate_per_kg", "truck_type"}
        load_signals = {"weight_kg", "from_city", "to_city", "cargo", "budget_per_kg", "material_type"}

        if any(data.get(signal) not in (None, "") for signal in truck_signals):
            return Intent.POST_TRUCK
        if any(data.get(signal) not in (None, "") for signal in load_signals):
            return Intent.CREATE_LOAD
        return None
