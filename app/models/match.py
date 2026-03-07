import uuid
from enum import Enum
from sqlalchemy import Column, Numeric, Boolean, DateTime, ForeignKey, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class MatchStatus(str, Enum):
    suggested = "suggested"
    accepted = "accepted"
    rejected = "rejected"
    completed = "completed"

class Match(Base):
    __tablename__ = "matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_id = Column(UUID(as_uuid=True), ForeignKey("truck_space_listings.id"), nullable=False)
    load_request_id = Column(UUID(as_uuid=True), ForeignKey("load_requests.id"), nullable=False)
    match_score = Column(Numeric(5, 2), nullable=False)
    agreed_price_per_kg = Column(Numeric(10, 2), nullable=True)
    shipper_confirmed = Column(Boolean, default=False, nullable=False)
    transporter_confirmed = Column(Boolean, default=False, nullable=False)
    status = Column(SQLEnum(MatchStatus), default=MatchStatus.suggested, nullable=False)
    matched_at = Column(DateTime(timezone=True), server_default=func.now())

    listing = relationship("TruckSpaceListing", back_populates="matches")
    load_request = relationship("LoadRequest", back_populates="matches")
