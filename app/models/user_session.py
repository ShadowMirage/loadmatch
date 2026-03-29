import uuid
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base

class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_user_sessions_session_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    session_id = Column(String(50), nullable=False, index=True)
    current_workflow = Column(String(100), nullable=True)
    step_number = Column(Integer, default=0, nullable=False)
    expected_input = Column(String(100), nullable=True)
    session_data = Column(JSONB, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="sessions")
