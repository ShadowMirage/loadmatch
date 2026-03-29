import uuid
from sqlalchemy import Column, Float, String, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class Rating(Base):
    __tablename__ = "ratings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), ForeignKey("matches.id"), nullable=False, index=True)
    rater_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    rated_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    score = Column(Float, nullable=False)
    comment = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    match = relationship("Match")
    rater = relationship("User", foreign_keys=[rater_id], back_populates="ratings_given")
    rated_user = relationship("User", foreign_keys=[rated_user_id], back_populates="ratings_received")
