import uuid
from enum import Enum
from sqlalchemy import Column, String, Integer, Numeric, Date, DateTime, ForeignKey, Text, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class LoadRequestStatus(str, Enum):
    open = "open"
    matched = "matched"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"

class LoadRequest(Base):
    __tablename__ = "load_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shipper_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    from_city = Column(String(100), nullable=False)
    to_city = Column(String(100), nullable=False)
    pickup_date = Column(Date, nullable=False)
    goods_type = Column(String(100), nullable=True)
    weight_kg = Column(Integer, nullable=False)
    budget_per_kg = Column(Numeric(10, 2), nullable=True)
    special_requirements = Column(Text, nullable=True)
    status = Column(SQLEnum(LoadRequestStatus), default=LoadRequestStatus.open, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    shipper = relationship("User", back_populates="load_requests")
    matches = relationship("Match", back_populates="load_request", cascade="all, delete-orphan")
