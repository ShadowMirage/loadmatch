"""
Pydantic extraction schemas for Stage 3: Pydantic-First Extraction Pipeline.

These models define the validated structure for LLM extraction outputs.
Used by ai_extraction_service.py via model_validate_json() before
falling back to regex or heuristic extraction.
"""
from typing import Optional
from pydantic import BaseModel, field_validator


class LoadExtraction(BaseModel):
    """Validated extraction for load/shipment requests."""
    action: str = "confirm_load_request"
    from_city: Optional[str] = None
    to_city: Optional[str] = None
    date: Optional[str] = None
    weight_kg: Optional[float] = None
    cargo: Optional[str] = None

    # Aliases from LLM output
    class Config:
        populate_by_name = True

    @field_validator("weight_kg", mode="before")
    @classmethod
    def normalize_weight(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = v.lower().replace(",", "")
            if "ton" in v:
                return float(v.split("ton")[0].strip()) * 1000
            return float(v)
        return float(v)


class TruckExtraction(BaseModel):
    """Validated extraction for truck/listing posts."""
    action: str = "confirm_truck_listing"
    plate: Optional[str] = None
    from_city: Optional[str] = None
    to_city: Optional[str] = None
    date: Optional[str] = None
    capacity_kg: Optional[float] = None
    rate_per_kg: Optional[float] = None

    class Config:
        populate_by_name = True

    @field_validator("capacity_kg", mode="before")
    @classmethod
    def normalize_capacity(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = v.lower().replace(",", "")
            if "ton" in v:
                return float(v.split("ton")[0].strip()) * 1000
            return float(v)
        return float(v)


class IntentResult(BaseModel):
    """Unified extraction result with confidence score."""
    action: str
    data: dict = {}
    field: Optional[str] = None
    confidence: float = 0.10  # Default: UNKNOWN-level confidence

    class Config:
        extra = "allow"
