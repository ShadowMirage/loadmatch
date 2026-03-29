import inspect
import json
import logging
import re

from app.config import settings
from app.runtime.ai_adapter import client_is_mocked, get_client
from app.services.date_parser import normalize_date
from app.services.weight_parser import normalize_weight

logger = logging.getLogger(__name__)

class RateLimitError(Exception):
    pass

def _client_is_mocked() -> bool:
    return client_is_mocked()

MODEL_NAME = "claude-3-haiku-20240307"

# ---------------------------------------------------------------------------
# PROMPT
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """
You are a logistics data extractor for a WhatsApp chatbot.

STRICT RULES:
- ALWAYS return valid JSON
- NEVER return null
- NEVER return explanations
- ONLY return a JSON object

SUPPORTED ACTIONS:
- confirm_truck_listing
- confirm_load_request
- ask_missing_field
- general_chat

FIELDS:

TRUCK:
- plate
- from
- to
- date
- capacity_kg

LOAD:
- from
- to
- date
- weight_kg
- cargo

DATE RULES:
- "tomorrow" → "tomorrow"
- "today" → "today"

----------------------------------------

USER MESSAGE (Contextualized):
{message}

STRICT RULE: The "New message" contains the user's latest intent. If it contradicts "Previous known data", the "New message" takes ABSOLUTE precedence.

RETURN JSON ONLY:
"""
def _fallback() -> dict:
    return {
        "action": "ask_missing_field",
        "field": "details",
        "data": {}
    }
# ---------------------------------------------------------------------------
# SAFE JSON PARSER (🔥 CRITICAL FIX)
# ---------------------------------------------------------------------------

def safe_extract(llm_response: str) -> dict:
    """
    Stage 3: Pydantic-First Extraction Pipeline.
    
    Attempt 1 → model_validate_json (confidence 0.95)
    Attempt 2 → regex boundary extraction (confidence 0.70)
    Attempt 3 → structural heuristic fallback (confidence 0.40)
    Attempt 4 → UNKNOWN fallback (confidence 0.10)
    """
    from app.contracts.extraction_schemas import IntentResult
    from pydantic import ValidationError

    if not llm_response:
        result = _fallback()
        result["confidence"] = 0.10
        return result

    # === ATTEMPT 1: Pydantic-First (Highest Confidence) ===
    try:
        parsed = IntentResult.model_validate_json(llm_response)
        result = parsed.model_dump()
        result["confidence"] = 0.95
        # Apply structure normalization
        result = _normalize_parsed_result(result)
        logger.debug(f"[PYDANTIC_PARSE] Success. confidence=0.95")
        return result
    except (ValidationError, Exception) as e:
        logger.debug(f"[PYDANTIC_PARSE] Failed: {e}. Falling back to regex.")

    # === ATTEMPT 2: Regex Boundary Extraction (Medium Confidence) ===
    try:
        match = re.search(r"(\{.*\})", llm_response, re.DOTALL)
        if match:
            text = match.group(1)
            parsed = json.loads(text)

            # Quantity / Capacity
            quantity = re.search(r"(\d+(?:\.\d+)?)\s*(ton|tons|kg|tonne|t)\b", text)
            if quantity:
                val = float(quantity.group(1))
                unit = quantity.group(2)
                if unit in ("ton", "tons", "tonne", "t"):
                    val *= 1000

            if isinstance(parsed, dict) and "action" in parsed:
                parsed["confidence"] = 0.70
                parsed = _normalize_parsed_result(parsed)
                logger.debug(f"[REGEX_PARSE] Success. confidence=0.70")
                return parsed
    except (json.JSONDecodeError, Exception) as e:
        logger.debug(f"[REGEX_PARSE] Failed: {e}. Falling back to heuristic.")

    # === ATTEMPT 3: Structural Heuristic Fallback (Low Confidence) ===
    try:
        match = re.search(r"(\{.*\})", llm_response, re.DOTALL)
        if match:
            text = match.group(1)
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                # Even without "action", try to infer from data
                if parsed.get("weight_kg") or parsed.get("from") or parsed.get("to"):
                    parsed.setdefault("action", "confirm_load_request")
                    parsed.setdefault("data", {})
                    parsed["confidence"] = 0.40
                    parsed = _normalize_parsed_result(parsed)
                    logger.debug(f"[HEURISTIC_PARSE] Success. confidence=0.40")
                    return parsed
    except Exception as e:
        logger.debug(f"[HEURISTIC_PARSE] Failed: {e}")

    # === ATTEMPT 4: UNKNOWN Fallback ===
    result = _fallback()
    result["confidence"] = 0.10
    logger.debug(f"[FALLBACK] All extraction methods failed. confidence=0.10")
    return result


def _normalize_parsed_result(parsed: dict) -> dict:
    """
    Applies structure normalization and action correction to parsed extraction results.
    Shared across all extraction attempts.
    """
    # Ensure data dict exists
    if "data" not in parsed or not isinstance(parsed.get("data"), dict):
        if parsed.get("action") == "confirm_truck_listing" and "truck" in parsed:
            parsed["data"] = parsed["truck"]
        elif parsed.get("action") == "confirm_load_request" and "load" in parsed:
            parsed["data"] = parsed["load"]
        else:
            parsed["data"] = {}

    # Merge top-level fields into data
    data = parsed["data"]
    for field in (
        "from_city",
        "to_city",
        "date",
        "weight_kg",
        "cargo",
        "capacity_kg",
        "rate_per_kg",
        "budget_per_kg",
        "plate",
        "current_city",
        "from",
        "to",
        "location",
    ):
        if parsed.get(field) is not None and field not in data:
            data[field] = parsed[field]

    # Action correction
    if data.get("weight_kg") and not data.get("capacity_kg"):
        parsed["action"] = "confirm_load_request"
    elif data.get("capacity_kg") or data.get("plate"):
        parsed["action"] = "confirm_truck_listing"

    if parsed["action"] == "ask_missing_field" and (
        data.get("from")
        or data.get("to")
        or data.get("from_city")
        or data.get("to_city")
    ):
        parsed["action"] = "confirm_load_request"

    if not data:
        parsed["action"] = "ask_missing_field"

    # Type-safe normalization
    if "weight_kg" in data and data["weight_kg"] is not None:
        val = data["weight_kg"]
        data["weight_kg"] = int(val) if isinstance(val, (int, float)) else normalize_weight(str(val))

    if "capacity_kg" in data and data["capacity_kg"] is not None:
        val = data["capacity_kg"]
        data["capacity_kg"] = int(val) if isinstance(val, (int, float)) else normalize_weight(str(val))

    if "date" in data and data["date"]:
        normalized_date = normalize_date(str(data["date"]))
        if normalized_date:
            data["date"] = normalized_date

    return parsed

# ---------------------------------------------------------------------------
# MAIN EXTRACTION
# ---------------------------------------------------------------------------

async def extract_with_context(message: str, session_data: dict) -> dict:
    """
    Session-aware extraction.
    ALWAYS returns valid dict.
    NEVER returns None.
    """

    try:
        if settings.is_placeholder(settings.ANTHROPIC_API_KEY) and not _client_is_mocked():
            return _fallback()

        context = f"""
Previous known data:
{json.dumps(session_data or {}, indent=2)}

New message:
{message}
"""

        prompt = EXTRACTION_PROMPT.format(message=context)

        from app.core.trace_context import get_trace_id

        client = get_client()
        if client is None:
            return _fallback()

        create_result = client.messages.create(
            model=MODEL_NAME,
            max_tokens=300,
            system="Return ONLY valid JSON. No text.",
            extra_headers={"X-Loadmatch-Trace-ID": get_trace_id()},
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        response = await create_result if inspect.isawaitable(create_result) else create_result

        llm_text = ""
        content_blocks = getattr(response, "content", None) or []
        if content_blocks:
            first_block = content_blocks[0]
            llm_text = str(getattr(first_block, "text", "") or "").strip()

        logger.debug(f"[RAW AI OUTPUT]: {llm_text}")

        result = safe_extract(llm_text)

        if not isinstance(result, dict):
            return _fallback()

        return result

    except Exception as e:
        logger.error(f"[AI EXTRACTION ERROR]: {e}")
        return _fallback()
