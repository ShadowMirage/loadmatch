import uuid
from sqlalchemy import Column, String, Boolean, DateTime, Float, Integer, JSON, func, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base
from app.models.enums import UserRole, KycFlowState

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone = Column(String(15), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=True)
    role = Column(SQLEnum(UserRole), nullable=True)
    language = Column(String(10), default="en", nullable=False)
    kyc_flow_state = Column(SQLEnum(KycFlowState), default=KycFlowState.not_started, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    wa_onboarded = Column(Boolean, default=False, nullable=False)
    shipper_rating = Column(Float, nullable=True)
    transporter_rating = Column(Float, nullable=True)
    completed_trips = Column(Integer, default=0, nullable=False)
    rating_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Intelligence Layer Fields
    memory_data = Column(JSON, default=dict, nullable=False)
    completion_rate = Column(Float, default=1.0, nullable=False)
    cancellation_rate = Column(Float, default=0.0, nullable=False)
    rating = Column(Float, default=5.0, nullable=False)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    events = relationship("EventLog", back_populates="user", cascade="all, delete-orphan")
    kyc_docs = relationship("KycDocument", back_populates="user", cascade="all, delete-orphan")
    trucks = relationship("Truck", back_populates="owner", cascade="all, delete-orphan")
    listings = relationship("TruckSpaceListing", back_populates="owner", cascade="all, delete-orphan")
    load_requests = relationship("LoadRequest", back_populates="shipper", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    ratings_received = relationship("Rating", foreign_keys="Rating.rated_user_id", back_populates="rated_user", cascade="all, delete-orphan")
    ratings_given = relationship("Rating", foreign_keys="Rating.rater_id", back_populates="rater", cascade="all, delete-orphan")
