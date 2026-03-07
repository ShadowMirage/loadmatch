import uuid
from enum import Enum
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class DocType(str, Enum):
    aadhaar = "aadhaar"
    pan = "pan"
    rc_book = "rc_book"
    driving_license = "driving_license"
    gst = "gst"

class KycDocument(Base):
    __tablename__ = "kyc_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    doc_type = Column(SQLEnum(DocType), nullable=False)
    doc_number = Column(String(50), nullable=True)
    file_url = Column(String(500), nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    rejection_reason = Column(Text, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="kyc_docs")
