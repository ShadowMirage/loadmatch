import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import MetaData, create_engine, inspect
from sqlalchemy.exc import IntegrityError

import sqlalchemy.dialects.sqlite.base as sqlite_base


# Keep the test self-contained so importing app settings does not require secrets.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("AWS_ACCESS_KEY", "test-key")
os.environ.setdefault("AWS_SECRET_KEY", "test-key")
os.environ.setdefault("AWS_REGION", "ap-south-1")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ADMIN_API_KEY", "test-key")

# SQLite has no native JSONB type, but the metadata still maps cleanly for schema tests.
sqlite_base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.database import Base  # noqa: E402
import app.models as app_models  # noqa: E402

assert app_models is not None


@pytest.fixture()
def engine():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    try:
        yield test_engine
    finally:
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def inspector(engine):
    return inspect(engine)


def _column_map(inspector, table_name):
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _assert_required_columns(inspector, table_name, required_columns):
    actual_columns = _column_map(inspector, table_name)
    missing = sorted(set(required_columns) - set(actual_columns))
    assert not missing, f"{table_name} is missing required columns: {missing}"
    return actual_columns


def _is_json_like(column_info):
    type_name = type(column_info["type"]).__name__.upper()
    compiled = str(column_info["type"]).upper()
    return "JSON" in type_name or "JSON" in compiled


def _has_unique_constraint(inspector, table_name, expected_columns):
    expected = list(expected_columns)

    for constraint in inspector.get_unique_constraints(table_name):
        if constraint.get("column_names") == expected:
            return True

    for index in inspector.get_indexes(table_name):
        if index.get("unique") and index.get("column_names") == expected:
            return True

    return False


def _reflect_table(engine, table_name):
    metadata = MetaData()
    metadata.reflect(bind=engine, only=[table_name])
    return metadata.tables[table_name]


def _auto_value(column, seed):
    type_name = type(column.type).__name__.upper()

    if column.name == "id" or column.name.endswith("_id") or "UUID" in type_name:
        return seed
    if "JSON" in type_name:
        return {"seed": seed}
    if "DATETIME" in type_name or "TIMESTAMP" in type_name:
        return datetime.now(timezone.utc)
    if "DATE" in type_name:
        return date.today()
    if "BOOLEAN" in type_name:
        return False
    if any(token in type_name for token in ("INTEGER", "SMALLINT", "BIGINT")):
        return seed
    if any(token in type_name for token in ("FLOAT", "NUMERIC", "DECIMAL")):
        return float(seed)
    return f"{column.name}_{seed}"


def _fill_required_columns(table, values, seed=1):
    completed = dict(values)

    for column in table.columns:
        if column.name in completed:
            continue
        if column.primary_key:
            continue
        if column.nullable:
            continue
        if column.default is not None or column.server_default is not None:
            continue
        completed[column.name] = _auto_value(column, seed)

    return completed


def _seed_user(connection, engine, suffix):
    users = _reflect_table(engine, "users")
    user_id = suffix
    values = {"id": user_id, "phone": f"91999999{suffix:04d}"}
    if "state" in users.c:
        values["state"] = "IDLE"
    values = _fill_required_columns(users, values, seed=suffix)
    connection.execute(users.insert().values(**values))
    return user_id


def _seed_load_request(connection, engine, user_id, suffix):
    load_requests = _reflect_table(engine, "load_requests")
    load_request_id = 1000 + suffix
    values = {
        "id": load_request_id,
        "from_city": "Jaipur",
        "to_city": "Delhi",
        "pickup_date": date.today() + timedelta(days=1),
        "weight_kg": 5000,
    }
    if "user_id" in load_requests.c:
        values["user_id"] = user_id
    if "shipper_id" in load_requests.c:
        values["shipper_id"] = user_id
    values = _fill_required_columns(load_requests, values, seed=suffix)
    connection.execute(load_requests.insert().values(**values))
    return load_request_id


def _seed_truck(connection, engine, user_id, suffix):
    trucks = _reflect_table(engine, "trucks")
    truck_id = 2000 + suffix
    values = {"id": truck_id}

    if "user_id" in trucks.c:
        values["user_id"] = user_id
    if "owner_id" in trucks.c:
        values["owner_id"] = user_id
    if "truck_type" in trucks.c:
        values["truck_type"] = "mini"
    if "capacity_kg" in trucks.c:
        values["capacity_kg"] = 9000
    if "total_capacity_kg" in trucks.c:
        values["total_capacity_kg"] = 9000
    if "location_city" in trucks.c:
        values["location_city"] = "Jaipur"
    if "available_from" in trucks.c:
        values["available_from"] = date.today()
    if "registration_number" in trucks.c:
        values["registration_number"] = f"TEST{suffix:06d}"

    values = _fill_required_columns(trucks, values, seed=suffix)
    connection.execute(trucks.insert().values(**values))
    return truck_id


def _seed_listing(connection, engine, truck_id, user_id, suffix):
    listings = _reflect_table(engine, "truck_space_listings")
    listing_id = 3000 + suffix
    values = {
        "id": listing_id,
        "truck_id": truck_id,
        "owner_id": user_id,
        "from_city": "Jaipur",
        "to_city": "Delhi",
        "departure_date": date.today() + timedelta(days=1),
        "total_capacity_kg": 9000,
        "available_capacity_kg": 4000,
        "price_per_kg": 10,
    }
    values = _fill_required_columns(listings, values, seed=suffix)
    connection.execute(listings.insert().values(**values))
    return listing_id


def _processed_message_values(table, user_id, wamid, replay_execution_hash=None):
    values = {
        "id": uuid.uuid4().int % 1_000_000_000,
        "wamid": wamid,
        "user_id": user_id,
        "intent": "CREATE_LOAD",
        "confidence": 0.98,
        "workflow_step": "INGESTED",
        "dispatcher_action": "ROUTE",
        "delivery_state": "PENDING",
        "execution_duration_ms": 1,
        "dispatch_started_at": datetime.now(timezone.utc),
        "retry_count": 0,
        "status": "IN_PROGRESS",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    if replay_execution_hash is not None:
        values["replay_execution_hash"] = replay_execution_hash

    if "idempotency_key" in table.c:
        values["idempotency_key"] = f"idem:{wamid}"
    if "trace_id" in table.c:
        values["trace_id"] = f"trace:{wamid}"
    if "expires_at" in table.c:
        values["expires_at"] = datetime.now(timezone.utc) + timedelta(days=1)

    return _fill_required_columns(table, values, seed=1)


def test_required_tables_exist(inspector):
    required_tables = {
        "users",
        "processed_messages",
        "workflow_events",
        "conversation_states",
        "load_requests",
        "trucks",
        "matches",
    }
    actual_tables = set(inspector.get_table_names())
    missing = sorted(required_tables - actual_tables)
    assert not missing, f"Missing required tables: {missing}"


def test_processed_messages_has_required_tier4_columns(inspector):
    _assert_required_columns(
        inspector,
        "processed_messages",
        [
            "wamid",
            "intent",
            "confidence",
            "workflow_step",
            "delivery_state",
            "dispatch_started_at",
            "delivered_at",
            "read_at",
            "failed_at",
            "replay_execution_hash",
        ],
    )


def test_wamid_is_unique(inspector):
    columns = _assert_required_columns(inspector, "processed_messages", ["wamid"])
    assert _has_unique_constraint(inspector, "processed_messages", ["wamid"]), (
        "processed_messages.wamid must be protected by a UNIQUE constraint or unique index "
        f"but current columns are {sorted(columns)}"
    )


def test_user_session_id_is_unique(inspector):
    columns = _assert_required_columns(inspector, "user_sessions", ["session_id"])
    assert _has_unique_constraint(inspector, "user_sessions", ["session_id"]), (
        "user_sessions.session_id must be protected by a UNIQUE constraint or unique index "
        f"but current columns are {sorted(columns)}"
    )


def test_workflow_events_schema(inspector):
    columns = _assert_required_columns(
        inspector,
        "workflow_events",
        ["processed_message_id", "payload", "state_snapshot_hash"],
    )
    assert _is_json_like(columns["payload"]), "workflow_events.payload must use JSONB/JSON storage"

    foreign_keys = inspector.get_foreign_keys("workflow_events")
    assert any(
        fk.get("constrained_columns") == ["processed_message_id"]
        and fk.get("referred_table") == "processed_messages"
        for fk in foreign_keys
    ), "workflow_events.processed_message_id must reference processed_messages.id"


def test_truck_space_listings_schema_has_canonical_lane_key(inspector):
    _assert_required_columns(
        inspector,
        "truck_space_listings",
        ["canonical_lane_key", "vehicle_type"],
    )


def test_load_requests_schema_has_canonical_lane_key(inspector):
    _assert_required_columns(
        inspector,
        "load_requests",
        ["canonical_lane_key", "vehicle_type"],
    )


def test_truck_space_listings_has_unique_user_lane_vehicle_open_index(inspector):
    indexes = inspector.get_indexes("truck_space_listings")
    unique_index = next((index for index in indexes if index.get("name") == "unique_user_lane_vehicle_open"), None)

    assert unique_index is not None, "truck_space_listings must expose unique_user_lane_vehicle_open"
    assert bool(unique_index.get("unique")) is True
    assert unique_index.get("column_names") == ["owner_id", "canonical_lane_key", "vehicle_type"]


def test_conversation_states_schema(inspector):
    columns = _assert_required_columns(
        inspector,
        "conversation_states",
        ["slot_state", "slot_versions", "checkpoint_hash"],
    )
    assert _is_json_like(columns["slot_state"]), "conversation_states.slot_state must use JSONB/JSON storage"
    assert _is_json_like(columns["slot_versions"]), "conversation_states.slot_versions must use JSONB/JSON storage"


def test_matches_ordering_is_deterministic(engine, inspector):
    _assert_required_columns(inspector, "matches", ["id", "score"])

    with engine.begin() as connection:
        user_id = _seed_user(connection, engine, suffix=1)
        load_request_id = _seed_load_request(connection, engine, user_id=user_id, suffix=1)
        truck_id = _seed_truck(connection, engine, user_id=user_id, suffix=1)

        matches = _reflect_table(engine, "matches")

        match_values = []
        highest_rank_id = 3
        for match_id in (
            2,
            1,
            highest_rank_id,
        ):
            row = {
                "id": match_id,
                "load_request_id": load_request_id,
                "score": 99.0 if match_id == highest_rank_id else 98.5,
            }
            if "truck_id" in matches.c:
                row["truck_id"] = truck_id
            if "listing_id" in matches.c:
                row["listing_id"] = _seed_listing(connection, engine, truck_id, user_id, suffix=match_id % 1000)
            if "status" in matches.c:
                row["status"] = "suggested"
            match_values.append(_fill_required_columns(matches, row, seed=1))

        connection.execute(matches.insert(), match_values)

        ordered_rows = connection.execute(
            sa.select(matches.c.id, matches.c.score).order_by(matches.c.score.desc(), matches.c.id.asc())
        ).all()

    ordered_ids = [row.id for row in ordered_rows]
    assert ordered_ids == [
        3,
        1,
        2,
    ]


def test_duplicate_wamid_raises_integrity_error(engine, inspector):
    _assert_required_columns(
        inspector,
        "processed_messages",
        ["wamid", "user_id", "replay_execution_hash"],
    )

    processed_messages = _reflect_table(engine, "processed_messages")

    with engine.begin() as connection:
        user_id = _seed_user(connection, engine, suffix=2)
        connection.execute(
            processed_messages.insert().values(
                **_processed_message_values(
                    processed_messages,
                    user_id=user_id,
                    wamid="wamid.duplicate.test",
                    replay_execution_hash="hash-1",
                )
            )
        )

    with engine.begin() as connection, pytest.raises(IntegrityError):
        user_id = _seed_user(connection, engine, suffix=3)
        connection.execute(
            processed_messages.insert().values(
                **_processed_message_values(
                    processed_messages,
                    user_id=user_id,
                    wamid="wamid.duplicate.test",
                    replay_execution_hash="hash-2",
                )
            )
        )


def test_stage5_requires_replay_execution_hash(engine, inspector):
    columns = _assert_required_columns(
        inspector,
        "processed_messages",
        ["wamid", "user_id", "replay_execution_hash"],
    )
    assert not columns["replay_execution_hash"]["nullable"], (
        "processed_messages.replay_execution_hash must be NOT NULL once Stage-5 determinism is active"
    )

    processed_messages = _reflect_table(engine, "processed_messages")

    with engine.begin() as connection, pytest.raises(IntegrityError):
        user_id = _seed_user(connection, engine, suffix=4)
        values = _processed_message_values(
            processed_messages,
            user_id=user_id,
            wamid="wamid.missing.hash",
        )
        values.pop("replay_execution_hash", None)
        connection.execute(
            processed_messages.insert().values(
                **values
            )
        )
