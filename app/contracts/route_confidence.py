from enum import Enum


class RouteConfidence(str, Enum):
    """
    Dispatcher-local pacing tier derived from resolver provenance.

    HIGH:
        Deterministic corridor match.

    MEDIUM:
        Adjacent shorthand match or structured LLM extraction.

    LOW:
        Regex extraction or fallback heuristics.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
