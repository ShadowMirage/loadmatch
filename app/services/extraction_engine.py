"""
ExtractionEngine is the single source of truth for:

- LLM extraction
- Regex fallback extraction
- Entity normalization
- Canonical schema enforcement

All downstream services MUST consume normalized entities
from ExtractionEngine outputs.
"""
import logging
import asyncio
import random
import re
from dataclasses import dataclass
from typing import Dict, Any

from app.contracts.extraction import ExtractionResult
from app.services.ai_extraction_service import extract_with_context, RateLimitError
from app.contracts.enums import Intent
from app.services.meta_intents import is_greeting
from app.runtime.redis_adapter import get_client as get_redis_client

logger = logging.getLogger(__name__)

def normalize_entities(data):
    if "from" in data and "from_city" not in data:
        data["from_city"] = data.pop("from")
    if "to" in data and "to_city" not in data:
        data["to_city"] = data.pop("to")
    if "weight" in data and "weight_kg" not in data:
        data["weight_kg"] = data.pop("weight")
    if "location" in data and "current_city" not in data:
        data["current_city"] = data.pop("location")
    if "city" in data and "current_city" not in data:
        data["current_city"] = data.pop("city")
    if "from_city" in data and "current_city" not in data:
        data["current_city"] = data["from_city"]
    return data

# Removed ExtractionResult — now centralized in app/contracts/extraction.py

class ExtractionEngine:
    # Circuit Breaker States
    CLOSED = "CLOSED"      # Normal (Use LLM)
    OPEN = "OPEN"          # Failed (Regex Fallback)
    HALF_OPEN = "HALF_OPEN" # Testing recovery

    FAILURE_THRESHOLD = 3
    RECOVERY_TIMEOUT = 60 # seconds
    HALF_OPEN_SUCCESS_REQUIRED = 2
    
    REDIS_KEY = "loadmatch:cb:anthropic"

    # Shared Redis client — prevents connection pool exhaustion under load
    _shared_redis = None

    @classmethod
    def _get_redis(cls):
        if cls._shared_redis is None:
            cls._shared_redis = get_redis_client()
        return cls._shared_redis

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self._redis = self._get_redis()

    # Text normalization moved to app/services/meta_intents.py or IntentResolver

    # Greeting check moved to app/services/meta_intents.py

    def _quick_extract(self, text: str) -> Dict[str, Any]:
        """Legacy regex-based extraction logic."""
        t = text.lower()
        data = {}
        
        # Route regex removed — now handled by IntentResolver heuristics

        # Quantity / Capacity
        quantity = re.search(r"(\d+(?:\.\d+)?)\s*(ton|tons|kg|tonne)", t)
        if quantity:
            val = float(quantity.group(1))
            if "ton" in quantity.group(2):
                val *= 1000
            if any(token in t for token in ("truck", "capacity", "space", "empty")):
                data["capacity_kg"] = val
            else:
                data["weight_kg"] = val

        rate = re.search(r"(?:rate|price|budget|asking(?: price)?)\s*(?:is|of)?\s*₹?\s*(\d+(?:\.\d+)?)\s*(?:/|per)?\s*kg", t)
        if rate:
            data["rate_per_kg"] = float(rate.group(1))

        date_match = re.search(r"\b(today|tomorrow)\b", t)
        if date_match:
            data["date"] = date_match.group(1)

        plate = re.search(r"\b([a-z]{2}\d{1,2}[a-z]{1,3}\d{4})\b", t)
        if plate:
            data["plate"] = plate.group(1).upper()

        return data


    async def extract(self, text: str, user: Any, session_data: Dict[str, Any]) -> ExtractionResult:
        """Main entry point for extraction with circuit breaker logic."""
        normalized_text = (text or "").strip().lower()

        if is_greeting(normalized_text):
            return ExtractionResult(
                intent=Intent.GREETING,
                data={},
                confidence=1.0,
                source="RULE",
                trace_id=self.trace_id,
            )

        # 1. Check Circuit Breaker
        use_llm = self._should_use_llm()
        
        # 2. Check User Quotas (Cost Guard)
        if use_llm and user and hasattr(user, "memory_data") and (user.memory_data or {}).get("llm_quota_exceeded", False):
            logger.warning(f"User {getattr(user, 'id', 'new')} quota exceeded. Falling back to Regex.")
            use_llm = False

        # 3. Execution
        if use_llm:
            for attempt in range(2):
                try:
                    # LLM Path (now returns confidence from Pydantic pipeline)
                    ai_result = await extract_with_context(normalized_text, session_data)
                    
                    # Update Circuit Breaker on Success
                    self._record_success()
                    
                    # Determine Intent
                    action = ai_result.get("action", "UNKNOWN")
                    intent = self._map_action_to_intent(action)
                    
                    # ✅ Stage 4: Propagate confidence from Pydantic pipeline
                    confidence = ai_result.get("confidence", 0.70)
                    
                    # ✅ Authoritative Normalization
                    raw_data = ai_result.get("data", {})
                    data = normalize_entities(raw_data)
                    
                    logger.debug("[ENTITY_SCHEMA] %s | confidence=%.2f", data, confidence)
                    
                    return ExtractionResult(
                        intent=intent,
                        data=data,
                        confidence=confidence,
                        source="LLM",
                        trace_id=self.trace_id
                    )
                except RateLimitError:
                    if attempt == 0:
                        wait = 0.3 + random.random() * 0.1
                        logger.warning(f"Anthropic Rate Limit hit. Retrying in {wait:.2f}s...")
                        await asyncio.sleep(wait)
                        continue
                    logger.error("Anthropic Rate Limit hit again. Falling back to Regex.")
                    self._record_failure()
                    break
                except Exception as e:
                    logger.error(f"LLM Extraction failed: {e}")
                    self._record_failure()
                    break

        # 4. Fallback Path (Regex) — confidence capped at 0.70
        regex_data = self._quick_extract(normalized_text)
        
        # ✅ Authoritative Normalization Fallback
        data = normalize_entities(regex_data)
        
        logger.debug("[ENTITY_SCHEMA] %s", data)
        
        # ✅ Stage 4: Regex/Heuristic mapping (ensuring IntentResolver can override)
        confidence = min(0.50 if data.get("from_city") and data.get("to_city") else 0.40, 0.50)
        
        # Only suggest CREATE_LOAD if we have both weight AND cities. 
        # If it's just weight, let IntentResolver decide based on context (it might be an update).
        suggested_intent = Intent.UNKNOWN
        if data.get("weight_kg") and data.get("from_city") and data.get("to_city"):
            suggested_intent = Intent.CREATE_LOAD

        current_workflow = session_data.get("current_workflow")

        # Intent Correction Guard: if we have extraction data and a workflow, assume it belongs to the workflow
        if current_workflow in ("LOAD_FLOW", "TRUCK_FLOW") and data:
             logger.info(f"[INTENT_RESOLVE] Contextual override for {current_workflow}")
             suggested_intent = Intent.CREATE_LOAD if current_workflow == "LOAD_FLOW" else Intent.POST_TRUCK

        return ExtractionResult(
            intent=suggested_intent,
            data=data,
            confidence=confidence,
            source="REGEX",
            trace_id=self.trace_id
        )

    def _should_use_llm(self) -> bool:
        if self._redis is None:
            return True
        try:
            state = self._redis.get(self.REDIS_KEY) or self.CLOSED
            return state != self.OPEN
        except Exception as e:
            logger.error(f"Redis CB failure: {e}")
            return True # Fallback to LLM if Redis is down

    def _record_failure(self):
        if self._redis is None:
            return
        try:
            # Simple increment and check
            fail_key = f"{self.REDIS_KEY}:failures"
            count = self._redis.incr(fail_key)
            if count == 1:
                self._redis.expire(fail_key, 300) # Reset failure window
            
            if count >= self.FAILURE_THRESHOLD:
                # OPEN the breaker with a 60s self-healing TTL
                self._redis.set(self.REDIS_KEY, self.OPEN, ex=self.RECOVERY_TIMEOUT)
                logger.error("Circuit Breaker OPENED (Cluster-wide)")
        except Exception as e:
            logger.error(f"Error recording failure in Redis: {e}")

    def _record_success(self):
        if self._redis is None:
            return
        try:
            # Clear failures on success if state is CLOSED
            state = self._redis.get(self.REDIS_KEY)
            if not state or state == self.CLOSED:
                self._redis.delete(f"{self.REDIS_KEY}:failures")
        except Exception as e:
            logger.error(f"Error recording success in Redis: {e}")

    def _map_action_to_intent(self, action: str) -> Intent:
        mapping = {
            "confirm_load_request": Intent.CREATE_LOAD,
            "confirm_truck_listing": Intent.POST_TRUCK,
            "general_chat": Intent.UNKNOWN
        }
        return mapping.get(action, Intent.UNKNOWN)
