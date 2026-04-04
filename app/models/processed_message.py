import uuid

from sqlalchemy import Column, DateTime, Enum as SQLEnum, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import synonym

from app.database import Base


EXECUTION_STATUS_VALUES = (
    "IN_PROGRESS",
    "EXECUTING",
    "SUCCESS",
    "FAILED",
    "REPLAYING",
    "DLQ",
)


class ProcessedMessage(Base):
    """
    Cluster-safe execution ledger with exactly-once delivery anchors and
    replay metadata. Legacy service code still refers to execution_started_at;
    keep that as a synonym over dispatch_started_at during the transition.
    """

    __tablename__ = "processed_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wamid = Column(String(128), unique=True, nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)

    idempotency_key = Column(String(255), unique=True, nullable=False, index=True)
    trace_id = Column(String(128), nullable=True, index=True)

    intent = Column(String(50), nullable=True, index=True)
    confidence = Column(Integer, nullable=True)
    workflow_step = Column(String(50), nullable=True)
    dispatcher_action = Column(String(50), nullable=True)
    delivery_state = Column(String(50), nullable=True)

    status = Column(
        SQLEnum(*EXECUTION_STATUS_VALUES, name="execution_status_enum"),
        default="IN_PROGRESS",
        nullable=False,
    )
    retry_count = Column(Integer, default=0)

    execution_owner = Column(UUID(as_uuid=True), nullable=True)
    recovery_attempted_at = Column(DateTime(timezone=True), nullable=True)
    tenant_id = Column(UUID(as_uuid=True), nullable=True)

    dispatch_started_at = Column(DateTime(timezone=True), nullable=True)
    execution_started_at = synonym("dispatch_started_at")

    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    execution_duration_ms = Column(Integer, nullable=True)

    replay_execution_hash = Column(String(128), nullable=False)

    # extraction_data is the canonical routing signal snapshot captured at
    # Phase 1 resolution time. It MUST remain stable across replay and recovery.
    # Dispatcher logic, recovery replay, and duplicate-delivery suppression rely
    # on this structure for deterministic behavior.
    #
    # Replay contract: request_payload["extraction_data"] is required for
    # deterministic replay stability and routing/audit fidelity.
    # Do not remove, rename, or recompute without a migration/backfill plan.
    request_payload = Column(JSONB, nullable=True)
    response_payload = Column(JSONB, nullable=True)
    error_log = Column(JSONB, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_retry_scan", "status", "retry_count"),
        Index("idx_recovery_scan", "status", "recovery_attempted_at", "updated_at"),
        Index("idx_workflow_step", "workflow_step"),
        Index("idx_delivered_at", "delivered_at"),
        Index("idx_read_at", "read_at"),
        Index("idx_failed_at", "failed_at"),
    )


class WorkflowEvent(Base):
    """
    Immutable workflow telemetry row. The FK allows replay reconstruction and
    automatic cleanup of orphan records.
    """

    __tablename__ = "workflow_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    processed_message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("processed_messages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    event_type = Column(String(50), nullable=False)
    trace_id = Column(String(128), nullable=True, index=True)
    payload = Column(JSONB, nullable=True)
    state_snapshot_hash = Column(String(128), nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
