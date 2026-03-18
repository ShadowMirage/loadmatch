import uuid
from sqlalchemy import Column, String, DateTime, Index, func
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class ProcessedMessage(Base):
    """Stores WhatsApp message IDs that have already been processed.
    Unique constraint on wa_message_id ensures deduplication is O(1).
    """
    __tablename__ = "processed_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wa_message_id = Column(String(128), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
