import uuid
from sqlalchemy import Column, String, Integer, Numeric, Date, DateTime, ForeignKey, Text, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base
from app.models.enums import LoadRequestStatus, CargoCategory

class LoadRequest(Base):
    __tablename__ = "load_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shipper_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    from_city = Column(String(100), nullable=False)
    to_city = Column(String(100), nullable=False)
    pickup_date = Column(Date, nullable=False)
    category = Column(SQLEnum(CargoCategory), nullable=True)
    goods_type = Column(String(100), nullable=True)
    weight_kg = Column(Integer, nullable=False)
    budget_per_kg = Column(Numeric(10, 2), nullable=True)
    special_requirements = Column(Text, nullable=True)
    status = Column(SQLEnum(LoadRequestStatus), default=LoadRequestStatus.open, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    shipper = relationship("User", back_populates="load_requests")
    matches = relationship("Match", back_populates="load_request", cascade="all, delete-orphan")
