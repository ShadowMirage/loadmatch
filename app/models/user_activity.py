import uuid
from sqlalchemy import Column, Integer, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class UserActivity(Base):
    """Tracks per-user message counts in a rolling 60-second window for rate limiting."""
    __tablename__ = "user_activity"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    message_count = Column(Integer, default=0, nullable=False)
    window_start = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")
