import uuid
from sqlalchemy import Column, String, Boolean, Integer, DateTime, ForeignKey, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base
from app.models.enums import TruckType

class Truck(Base):
    __tablename__ = "trucks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    registration_number = Column(String(20), unique=True, nullable=False)
    truck_type = Column(SQLEnum(TruckType), nullable=False)
    total_capacity_kg = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="trucks")
    listings = relationship("TruckSpaceListing", back_populates="truck", cascade="all, delete-orphan")
