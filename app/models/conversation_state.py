import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class ConversationState(Base):
    __tablename__ = "conversation_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    active_intent = Column(String(50), nullable=True, index=True)
    intent_confidence = Column(Integer, nullable=True)

    slot_state = Column(JSONB, nullable=True)
    slot_versions = Column(JSONB, nullable=True)

    clarification_count = Column(Integer, default=0, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)

    workflow_step = Column(String(50), nullable=True)
    checkpoint_hash = Column(String(128), nullable=True)
    last_transition = Column(String(100), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
