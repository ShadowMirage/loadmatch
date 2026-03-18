import uuid
from sqlalchemy import Column, String, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class RouteSubscription(Base):
    """Stores user subscriptions to specific freight corridors.
    When a new load or truck appears on a subscribed route, the user is notified.
    """
    __tablename__ = "route_subscriptions"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    pickup_city    = Column(String(100), nullable=False)   # display form e.g. "Jaipur"
    drop_city      = Column(String(100), nullable=False)   # display form e.g. "Delhi"
    normalized_pickup = Column(String(100), nullable=False)
    normalized_drop   = Column(String(100), nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")

    # Composite index so corridor lookups are O(log n)
    __table_args__ = (
        Index("idx_route_sub_corridor", "normalized_pickup", "normalized_drop"),
    )
