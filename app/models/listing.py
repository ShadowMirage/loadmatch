import uuid
from enum import Enum
from sqlalchemy import Column, String, Integer, Numeric, Date, DateTime, ForeignKey, Text, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class ListingStatus(str, Enum):
    open = "open"
    partial = "partial"
    full = "full"
    completed = "completed"
    cancelled = "cancelled"

class TruckSpaceListing(Base):
    __tablename__ = "truck_space_listings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    truck_id = Column(UUID(as_uuid=True), ForeignKey("trucks.id"), nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    from_city = Column(String(100), nullable=False)
    to_city = Column(String(100), nullable=False)
    departure_date = Column(Date, nullable=False)
    total_capacity_kg = Column(Integer, nullable=False)
    available_capacity_kg = Column(Integer, nullable=False)
    price_per_kg = Column(Numeric(10, 2), nullable=False)
    status = Column(SQLEnum(ListingStatus), default=ListingStatus.open, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    truck = relationship("Truck", back_populates="listings")
    owner = relationship("User", back_populates="listings")
    matches = relationship("Match", back_populates="listing", cascade="all, delete-orphan")
