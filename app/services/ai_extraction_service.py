import json
import logging
import re
from anthropic import AsyncAnthropic
from app.config import settings
from app.services.weight_parser import normalize_weight

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

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

USER MESSAGE:
{message}

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
    if not llm_response:
        return _fallback()

    try:
        text = llm_response.strip()

        # Remove markdown
        text = re.sub(r"```json", "", text)
        text = re.sub(r"```", "", text)

        # Remove garbage before JSON
        text = re.sub(r"^[^{]*", "", text)

        # Ensure valid JSON boundaries
        if not text.startswith("{"):
            text = "{" + text
        if not text.endswith("}"):
            text = text + "}"

        parsed = json.loads(text)

        # BASIC VALIDATION
        if not isinstance(parsed, dict):
            return _fallback()

        if "action" not in parsed:
            return _fallback()

        # ------------------------------------------------------------------
        # STRUCTURE NORMALIZATION
        # ------------------------------------------------------------------

        if "data" not in parsed or not isinstance(parsed["data"], dict):

            if parsed.get("action") == "confirm_truck_listing" and "truck" in parsed:
                parsed["data"] = parsed["truck"]

            elif parsed.get("action") == "confirm_load_request" and "load" in parsed:
                parsed["data"] = parsed["load"]

            else:
                parsed["data"] = {}

        # Merge top-level fields
        if "rate_per_kg" in parsed:
            parsed["data"]["rate_per_kg"] = parsed["rate_per_kg"]

        data = parsed["data"]

        # ------------------------------------------------------------------
        # ACTION CORRECTION
        # ------------------------------------------------------------------

        if data.get("plate"):
            parsed["action"] = "confirm_truck_listing"

        elif data.get("weight_kg"):
            parsed["action"] = "confirm_load_request"

        # ------------------------------------------------------------------
        # FINAL SAFETY
        # ------------------------------------------------------------------

        if not data:
            parsed["action"] = "ask_missing_field"

        # ------------------------------------------------------------------
        # TYPE-SAFE NORMALIZATION
        # ------------------------------------------------------------------

        if "weight_kg" in data and data["weight_kg"] is not None:
            val = data["weight_kg"]
            data["weight_kg"] = int(val) if isinstance(val, (int, float)) else normalize_weight(str(val))

        if "capacity_kg" in data and data["capacity_kg"] is not None:
            val = data["capacity_kg"]
            data["capacity_kg"] = int(val) if isinstance(val, (int, float)) else normalize_weight(str(val))

        return parsed

    except Exception as e:
        logger.error(f"[AI PARSE ERROR] Raw: {llm_response} | Error: {e}")
        return _fallback()

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
        context = f"""
Previous known data:
{json.dumps(session_data or {}, indent=2)}

New message:
{message}
"""

        prompt = EXTRACTION_PROMPT.format(message=context)

        response = await client.messages.create(
            model=MODEL_NAME,
            max_tokens=300,
            system="Return ONLY valid JSON. No text.",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        llm_text = ""
        if response.content and len(response.content) > 0:
            llm_text = response.content[0].text.strip()

        logger.debug(f"[RAW AI OUTPUT]: {llm_text}")

        result = safe_extract(llm_text)

        if not isinstance(result, dict):
            return _fallback()

        return result

    except Exception as e:
        logger.error(f"[AI EXTRACTION ERROR]: {e}")
        return _fallback()