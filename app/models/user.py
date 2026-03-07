import uuid
from enum import Enum
from sqlalchemy import Column, String, Boolean, DateTime, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class UserRole(str, Enum):
    shipper = "shipper"
    transporter = "transporter"
    both = "both"

class KycStatus(str, Enum):
    pending = "pending"
    under_review = "under_review"
    verified = "verified"
    rejected = "rejected"

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone = Column(String(15), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=True)
    role = Column(SQLEnum(UserRole), nullable=True)
    kyc_status = Column(SQLEnum(KycStatus), default=KycStatus.pending, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    wa_onboarded = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    kyc_docs = relationship("KycDocument", back_populates="user", cascade="all, delete-orphan")
    trucks = relationship("Truck", back_populates="owner", cascade="all, delete-orphan")
    listings = relationship("TruckSpaceListing", back_populates="owner", cascade="all, delete-orphan")
    load_requests = relationship("LoadRequest", back_populates="shipper", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
