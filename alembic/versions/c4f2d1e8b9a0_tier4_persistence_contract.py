"""tier4_persistence_contract

Revision ID: c4f2d1e8b9a0
Revises: 76053ffd589b
Create Date: 2026-03-25 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c4f2d1e8b9a0"
down_revision: Union[str, Sequence[str], None] = "76053ffd589b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EXECUTION_STATUS_VALUES = (
    "IN_PROGRESS",
    "EXECUTING",
    "SUCCESS",
    "FAILED",
    "REPLAYING",
    "DLQ",
)


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)}


def _has_fk(table_name: str, constrained_columns: list[str], referred_table: str) -> bool:
    for fk in inspect(op.get_bind()).get_foreign_keys(table_name):
        if fk.get("constrained_columns") == constrained_columns and fk.get("referred_table") == referred_table:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    processed_columns = _columns("processed_messages")
    workflow_columns = _columns("workflow_events")
    match_columns = _columns("matches")

    if "conversation_states" not in inspect(bind).get_table_names():
        op.create_table(
            "conversation_states",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("active_intent", sa.String(length=50), nullable=True),
            sa.Column("intent_confidence", sa.Integer(), nullable=True),
            sa.Column("slot_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("slot_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("clarification_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("workflow_step", sa.String(length=50), nullable=True),
            sa.Column("checkpoint_hash", sa.String(length=128), nullable=True),
            sa.Column("last_transition", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_conversation_states_user", "conversation_states", ["user_id"], unique=False)
        op.create_index("idx_conversation_states_intent", "conversation_states", ["active_intent"], unique=False)

    if "wamid" not in processed_columns:
        op.add_column("processed_messages", sa.Column("wamid", sa.String(length=128), nullable=True))
    if "user_id" not in processed_columns:
        op.add_column("processed_messages", sa.Column("user_id", sa.UUID(), nullable=True))
    if "intent" not in processed_columns:
        op.add_column("processed_messages", sa.Column("intent", sa.String(length=50), nullable=True))
    if "confidence" not in processed_columns:
        op.add_column("processed_messages", sa.Column("confidence", sa.Integer(), nullable=True))
    if "workflow_step" not in processed_columns:
        op.add_column("processed_messages", sa.Column("workflow_step", sa.String(length=50), nullable=True))
    if "dispatcher_action" not in processed_columns:
        op.add_column("processed_messages", sa.Column("dispatcher_action", sa.String(length=50), nullable=True))
    if "delivery_state" not in processed_columns:
        op.add_column("processed_messages", sa.Column("delivery_state", sa.String(length=50), nullable=True))

    if dialect == "postgresql":
        op.execute(
            """
            UPDATE processed_messages
            SET wamid = COALESCE(
                wamid,
                request_payload->>'wa_id',
                NULLIF(split_part(idempotency_key, ':', 3), ''),
                'synthetic.' || substr(md5(COALESCE(idempotency_key, id::text)), 1, 24)
            )
            WHERE wamid IS NULL
            """
        )
        op.execute(
            """
            UPDATE processed_messages
            SET intent = COALESCE(intent, request_payload->>'intent'),
                workflow_step = COALESCE(workflow_step, 'IDLE'),
                dispatcher_action = COALESCE(dispatcher_action, request_payload->>'intent'),
                delivery_state = COALESCE(delivery_state, CASE WHEN delivered_at IS NOT NULL THEN 'DELIVERED' ELSE 'PENDING' END),
                confidence = COALESCE(confidence, 100),
                replay_execution_hash = COALESCE(
                    replay_execution_hash,
                    md5(
                        COALESCE(wamid, '') ||
                        COALESCE(idempotency_key, '') ||
                        COALESCE(request_payload::text, '')
                    )
                )
            """
        )
    else:
        op.execute(
            """
            UPDATE processed_messages
            SET wamid = COALESCE(wamid, idempotency_key, 'synthetic'),
                intent = COALESCE(intent, 'UNKNOWN'),
                workflow_step = COALESCE(workflow_step, 'IDLE'),
                dispatcher_action = COALESCE(dispatcher_action, intent),
                delivery_state = COALESCE(delivery_state, 'PENDING'),
                confidence = COALESCE(confidence, 100),
                replay_execution_hash = COALESCE(replay_execution_hash, 'legacy-hash')
            """
        )

    if dialect == "postgresql":
        execution_status_enum = postgresql.ENUM(*EXECUTION_STATUS_VALUES, name="execution_status_enum")
        execution_status_enum.create(bind, checkfirst=True)
        op.execute("UPDATE processed_messages SET status = 'DLQ' WHERE status = 'DEAD_LETTER'")
        op.execute(
            """
            ALTER TABLE processed_messages
            ALTER COLUMN status TYPE execution_status_enum
            USING status::execution_status_enum
            """
        )

    op.alter_column("processed_messages", "wamid", existing_type=sa.String(length=128), nullable=False)
    op.alter_column(
        "processed_messages",
        "replay_execution_hash",
        existing_type=sa.String(length=128),
        nullable=False,
    )

    if not _has_fk("processed_messages", ["user_id"], "users"):
        op.create_foreign_key(
            "fk_processed_messages_user_id",
            "processed_messages",
            "users",
            ["user_id"],
            ["id"],
        )

    processed_indexes = _indexes("processed_messages")
    if "ix_processed_messages_wamid" not in processed_indexes:
        op.create_index("ix_processed_messages_wamid", "processed_messages", ["wamid"], unique=True)
    if "ix_processed_messages_intent" not in processed_indexes:
        op.create_index("ix_processed_messages_intent", "processed_messages", ["intent"], unique=False)
    if "idx_workflow_step" not in processed_indexes:
        op.create_index("idx_workflow_step", "processed_messages", ["workflow_step"], unique=False)

    if "trace_id" not in workflow_columns:
        op.add_column("workflow_events", sa.Column("trace_id", sa.String(length=50), nullable=True))
    workflow_indexes = _indexes("workflow_events")
    if "ix_workflow_events_trace_id" not in workflow_indexes:
        op.create_index("ix_workflow_events_trace_id", "workflow_events", ["trace_id"], unique=False)

    if dialect == "postgresql":
        op.execute(
            """
            DELETE FROM workflow_events we
            WHERE NOT EXISTS (
                SELECT 1
                FROM processed_messages pm
                WHERE pm.id = we.processed_message_id
            )
            """
        )
    if not _has_fk("workflow_events", ["processed_message_id"], "processed_messages"):
        op.create_foreign_key(
            "fk_workflow_events_processed_message_id",
            "workflow_events",
            "processed_messages",
            ["processed_message_id"],
            ["id"],
            ondelete="CASCADE",
        )

    if "match_score" in match_columns and "score" not in match_columns:
        op.alter_column("matches", "match_score", new_column_name="score")

    if "idx_execution_reclaim" in _indexes("processed_messages"):
        op.drop_index("idx_execution_reclaim", table_name="processed_messages")
    op.create_index(
        "idx_execution_reclaim",
        "processed_messages",
        ["status", "dispatch_started_at"],
        unique=False,
        postgresql_where=sa.text("status = 'EXECUTING'"),
    )


def downgrade() -> None:
    if "idx_execution_reclaim" in _indexes("processed_messages"):
        op.drop_index("idx_execution_reclaim", table_name="processed_messages")

    if "score" in _columns("matches") and "match_score" not in _columns("matches"):
        op.alter_column("matches", "score", new_column_name="match_score")

    if _has_fk("workflow_events", ["processed_message_id"], "processed_messages"):
        op.drop_constraint("fk_workflow_events_processed_message_id", "workflow_events", type_="foreignkey")

    workflow_indexes = _indexes("workflow_events")
    if "ix_workflow_events_trace_id" in workflow_indexes:
        op.drop_index("ix_workflow_events_trace_id", table_name="workflow_events")
    if "trace_id" in _columns("workflow_events"):
        op.drop_column("workflow_events", "trace_id")

    processed_indexes = _indexes("processed_messages")
    if "idx_workflow_step" in processed_indexes:
        op.drop_index("idx_workflow_step", table_name="processed_messages")
    if "ix_processed_messages_intent" in processed_indexes:
        op.drop_index("ix_processed_messages_intent", table_name="processed_messages")
    if "ix_processed_messages_wamid" in processed_indexes:
        op.drop_index("ix_processed_messages_wamid", table_name="processed_messages")

    if _has_fk("processed_messages", ["user_id"], "users"):
        op.drop_constraint("fk_processed_messages_user_id", "processed_messages", type_="foreignkey")

    for column_name in ("delivery_state", "dispatcher_action", "workflow_step", "confidence", "intent", "user_id", "wamid"):
        if column_name in _columns("processed_messages"):
            op.drop_column("processed_messages", column_name)

    if "conversation_states" in inspect(op.get_bind()).get_table_names():
        if "idx_conversation_states_intent" in _indexes("conversation_states"):
            op.drop_index("idx_conversation_states_intent", table_name="conversation_states")
        if "idx_conversation_states_user" in _indexes("conversation_states"):
            op.drop_index("idx_conversation_states_user", table_name="conversation_states")
        op.drop_table("conversation_states")
