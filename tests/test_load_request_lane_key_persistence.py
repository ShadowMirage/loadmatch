import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sqlalchemy as sa

from app.models.load_request import LoadRequest
from app.services.dispatcher_service import DispatcherService
from app.services.matching_service import compute_lane_demand_score


def _load_migration_module():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "f2a3b4c5d6e7_persist_load_request_lane_key.py"
    )
    spec = importlib.util.spec_from_file_location("persist_load_request_lane_key", migration_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


migration = _load_migration_module()


def test_loadrequest_lane_key_persisted():
    db = MagicMock()
    db.flush.return_value = None
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "Delhi NCR",
            "to_city": "Jaipur",
            "weight_kg": 5000,
            "date": "tomorrow",
        }
    )

    with patch(
        "app.services.dispatcher_service.find_matches_for_load_summary",
        return_value={"match_count": 0, "matches": []},
    ):
        dispatcher._handle_confirm_load(payload)

    added_objects = [call.args[0] for call in db.add.call_args_list]
    load = next(obj for obj in added_objects if isinstance(obj, LoadRequest))
    assert load.canonical_lane_key == "delhi:jaipur"


def test_loadrequest_vehicle_type_persisted_when_present():
    db = MagicMock()
    db.flush.return_value = None
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "Delhi",
            "to_city": "Jaipur",
            "weight_kg": 5000,
            "date": "tomorrow",
            "vehicle_type": "Trailer",
        }
    )

    with patch(
        "app.services.dispatcher_service.find_matches_for_load_summary",
        return_value={"match_count": 0, "matches": []},
    ):
        dispatcher._handle_confirm_load(payload)

    added_objects = [call.args[0] for call in db.add.call_args_list]
    load = next(obj for obj in added_objects if isinstance(obj, LoadRequest))
    assert load.vehicle_type == "trailer"


def test_loadrequest_backfill_complete():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    load_requests = sa.Table(
        "load_requests",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("from_city", sa.String),
        sa.Column("to_city", sa.String),
        sa.Column("canonical_lane_key", sa.String),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            load_requests.insert(),
            [
                {"id": "load-1", "from_city": "Delhi NCR", "to_city": "Jaipur", "canonical_lane_key": None},
                {"id": "load-2", "from_city": "blr", "to_city": "delhi", "canonical_lane_key": None},
                {"id": "load-3", "from_city": "Mumbai", "to_city": None, "canonical_lane_key": None},
                {"id": "load-4", "from_city": "Jaipur", "to_city": "Delhi", "canonical_lane_key": "delhi:jaipur"},
            ],
        )

        stats = migration._backfill_load_request_lane_keys(connection)

        row_1_lane = connection.execute(
            sa.text("SELECT canonical_lane_key FROM load_requests WHERE id = 'load-1'")
        ).scalar_one()
        row_2_lane = connection.execute(
            sa.text("SELECT canonical_lane_key FROM load_requests WHERE id = 'load-2'")
        ).scalar_one()
        remaining_backfillable = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM load_requests "
                "WHERE canonical_lane_key IS NULL "
                "AND from_city IS NOT NULL "
                "AND to_city IS NOT NULL"
            )
        ).scalar_one()

    assert stats == {"scanned_rows": 3, "updated_rows": 2}
    assert row_1_lane == "delhi:jaipur"
    assert row_2_lane == "bangalore:delhi"
    assert remaining_backfillable == 0


def test_matching_uses_persisted_load_lane_key():
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = [
        SimpleNamespace(
            id="load-1",
            from_city="Mumbai",
            to_city="Chennai",
            canonical_lane_key="delhi:jaipur",
            created_at=datetime.now(timezone.utc),
        )
    ]
    session = MagicMock()
    session.query.return_value = query

    with patch(
        "app.services.matching_service.canonical_lane_key",
        side_effect=AssertionError("runtime lane derivation should not run when persisted lane key exists"),
    ):
        score = compute_lane_demand_score(session, "delhi:jaipur")

    assert score == 2


def test_dispatcher_uses_ranking_context_timestamp_for_matching():
    db = MagicMock()
    db.flush.return_value = None
    dispatcher = DispatcherService(db, user_id="user-123")
    ranking_timestamp = "2026-04-08T10:00:00+00:00"
    payload = SimpleNamespace(
        data={
            "from_city": "Delhi",
            "to_city": "Jaipur",
            "weight_kg": 5000,
            "date": "tomorrow",
            "ranking_context_timestamp": ranking_timestamp,
        }
    )

    with patch(
        "app.services.dispatcher_service.find_matches_for_load_summary",
        return_value={"match_count": 0, "matches": []},
    ) as mock_summary:
        dispatcher._handle_confirm_load(payload)

    assert mock_summary.call_count == 1
    now_arg = mock_summary.call_args.kwargs["now"]
    assert now_arg == datetime.fromisoformat(ranking_timestamp)
