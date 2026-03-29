from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Dict, Any

@dataclass
class CreateLoadPayload:
    from_city: str
    to_city: str
    weight_kg: float
    material_type: Optional[str] = None
    pickup_date: Optional[date] = None
    budget_per_kg: Optional[float] = None
    
    def __post_init__(self):
        if not self.from_city or not self.to_city:
            raise ValueError("From and To cities are required for CREATE_LOAD")

@dataclass
class PostTruckPayload:
    capacity_kg: float
    current_city: str
    to_city: Optional[str] = None
    rate_per_kg: Optional[float] = None
    plate: Optional[str] = None
    truck_type: Optional[str] = "Open Body"
    departure_date: Optional[date] = None
    
    def __post_init__(self):
        if not self.current_city:
            raise ValueError("Current city is required for POST_TRUCK")

@dataclass
class GenericActionPayload:
    action: str
    data: Dict[str, Any] = field(default_factory=dict)
