import uuid
from sqlalchemy import Column, String, Integer, Numeric, Date, DateTime, ForeignKey, Text, func, Enum as SQLEnum, Index, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base
from app.models.enums import ListingStatus

class TruckSpaceListing(Base):
    __tablename__ = "truck_space_listings"
    __table_args__ = (
        Index(
            "unique_user_lane_vehicle_open",
            "owner_id",
            "canonical_lane_key",
            "vehicle_type",
            unique=True,
            postgresql_where=text("status IN ('open','partial')"),
            sqlite_where=text("status IN ('open','partial')"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    truck_id = Column(UUID(as_uuid=True), ForeignKey("trucks.id"), nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    from_city = Column(String(100), nullable=False)
    to_city = Column(String(100), nullable=False)
    canonical_lane_key = Column(String(255), nullable=True, index=True)
    vehicle_type = Column(String(32), nullable=True, index=True)
    departure_date = Column(Date, nullable=False)
    total_capacity_kg = Column(Integer, nullable=False)
    available_capacity_kg = Column(Integer, nullable=False)
    allowed_categories = Column(JSONB, nullable=True)
    price_per_kg = Column(Numeric(10, 2), nullable=False)
    status = Column(SQLEnum(ListingStatus), default=ListingStatus.open, nullable=False)
    notes = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    truck = relationship("Truck", back_populates="listings")
    owner = relationship("User", back_populates="listings")
    matches = relationship("Match", back_populates="listing", cascade="all, delete-orphan")
