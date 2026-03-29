from dataclasses import dataclass
from typing import Dict, Any, Optional
from app.contracts.enums import Intent

@dataclass
class ExtractionResult:
    intent: Intent
    data: Dict[str, Any]
    confidence: float
    source: str  # 'REGEX', 'LLM', 'MEMORY', 'CACHE'
    trace_id: str
