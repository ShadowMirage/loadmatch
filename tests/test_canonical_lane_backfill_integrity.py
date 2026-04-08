import asyncio
import importlib.util
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.enums import Intent
from app.contracts.payloads import GenericActionPayload
from app.models.listing import TruckSpaceListing
from app.models.truck import Truck
from app.services.dispatcher_service import DispatcherService
from app.services.matching_service import (
    canonical_lane_key,
    find_recent_duplicate_listing,
    listing_canonical_lane_key,
)
from app.services.recovery_daemon import RecoveryDaemon


def _load_migration_module():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "e6f7a8b9c0d1_persist_canonical_lane_key.py"
    )
    spec = importlib.util.spec_from_file_location("persist_canonical_lane_key", migration_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


migration = _load_migration_module()


class _FakeQuery:
    def __init__(self, model, storage):
        self.model = model
        self.storage = storage

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self.storage.get(self.model, []))

    def first(self):
        rows = self.storage.get(self.model, [])
        return rows[0] if rows else None


class _FakeDB:
    def __init__(self):
        self.storage = {}

    def add(self, obj):
        self.storage.setdefault(type(obj), []).append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def flush(self):
        pass

    def query(self, model):
        return _FakeQuery(model, self.storage)


def _query_all(rows):
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = rows
    return query


def test_alias_backfill_matches_runtime_normalization():
    assert migration._canonical_lane_key("baroda", "blr") == canonical_lane_key("vadodara", "bangalore")
    assert migration._canonical_lane_key("baroda", "blr") == "bangalore:vadodara"


def test_directional_backfill_symmetry():
    assert migration._canonical_lane_key("delhi", "jaipur") == "delhi:jaipur"
    assert migration._canonical_lane_key("jaipur", "delhi") == "delhi:jaipur"


def test_missing_city_rows_not_backfilled():
    assert migration._canonical_lane_key(None, "jaipur") is None
    assert migration._canonical_lane_key("delhi", None) is None
    assert migration._canonical_lane_key("", "jaipur") is None


def test_runtime_fallback_used_when_persisted_missing():
    listing = SimpleNamespace(
        id="listing-1",
        canonical_lane_key=None,
        from_city="blr",
        to_city="delhi",
    )

    with patch("app.services.matching_service.logger.info") as mock_info, \
         patch("app.services.matching_service.logger.warning") as mock_warning:
        lane_key = listing_canonical_lane_key(listing)

    assert lane_key == "bangalore:delhi"
    info_messages = [call.args[0] for call in mock_info.call_args_list]
    warning_messages = [call.args[0] for call in mock_warning.call_args_list]
    assert "LANE_KEY_BACKFILL_FALLBACK_USED" in warning_messages
    assert "PERSISTED_LANE_KEY_FALLBACK_USED" in info_messages


def test_duplicate_detection_with_null_persisted_lane_key():
    listing = SimpleNamespace(
        id="listing-2",
        owner_id="user-123",
        canonical_lane_key=None,
        from_city="Delhi NCR",
        to_city="Jaipur",
        created_at=datetime.now(timezone.utc),
        status="open",
    )
    db = MagicMock()
    db.query.return_value = _query_all([listing])

    duplicate = find_recent_duplicate_listing(db, "user-123", "delhi", "jaipur")

    assert duplicate is listing


def test_route_correction_updates_persisted_lane_key():
    db = MagicMock()
    db.flush.return_value = None
    listing_query = MagicMock()
    listing_query.filter.return_value = listing_query
    listing_query.all.return_value = []
    truck_query = MagicMock()
    truck_query.filter.return_value = truck_query
    truck_query.first.return_value = None
    db.query.side_effect = [listing_query, truck_query]
    dispatcher = DispatcherService(db, user_id="user-123", phone="919999999999")
    payload = GenericActionPayload(
        action="CONFIRM",
        data={
            "current_city": "delhi",
            "to_city": "mumbai",
            "capacity_kg": 7000,
            "departure_date": "tomorrow",
            "lane_key": "delhi:mumbai",
            "directional_lane_key": "delhi->mumbai",
        },
    )

    with patch(
        "app.services.dispatcher_service.get_session_data",
        return_value={
            "current_city": "delhi",
            "to_city": "jaipur",
            "lane_key": "delhi:jaipur",
            "directional_lane_key": "delhi->jaipur",
        },
    ), patch(
        "app.services.dispatcher_service.find_matches_for_truck_summary",
        return_value={"match_count": 0, "matches": []},
    ):
        dispatcher.execute(Intent.CONFIRM, payload, current_workflow="TRUCK_FLOW")

    added_objects = [call.args[0] for call in db.add.call_args_list]
    listing = next(obj for obj in added_objects if hasattr(obj, "canonical_lane_key"))
    assert listing.canonical_lane_key == "delhi:mumbai"


def test_recovery_replay_persists_lane_key():
    db = _FakeDB()
    record = SimpleNamespace(
        id="pm-recovery",
        idempotency_key="user-123:CONFIRM:wamid.recovery:TRUCK_FLOW",
        trace_id="trace-recovery",
        request_payload={
            "intent": "CONFIRM",
            "payload": {"action": "CONFIRM", "data": {}},
            "extraction_data": {
                "current_city": "blr",
                "to_city": "delhi",
                "capacity_kg": 7000,
                "departure_date": "tomorrow",
                "lane_key": "bangalore:delhi",
                "directional_lane_key": "bangalore->delhi",
            },
            "current_workflow": "TRUCK_FLOW",
        },
        status="EXECUTING",
        retry_count=0,
        execution_owner=None,
        error_log=None,
        response_payload=None,
        workflow_step=None,
        delivery_state=None,
        execution_started_at=None,
        execution_duration_ms=None,
    )

    async def run():
        daemon = RecoveryDaemon(lambda: db)
        with patch(
            "app.services.dispatcher_service.find_matches_for_truck_summary",
            return_value={"match_count": 0, "matches": []},
        ):
            await daemon._replay_record(db, record)

    asyncio.run(run())

    listings = db.storage.get(TruckSpaceListing, [])
    trucks = db.storage.get(Truck, [])
    assert trucks, "recovery replay must create or hydrate the truck before listing persistence"
    assert listings, "recovery replay must persist a truck-space listing"
    assert listings[0].canonical_lane_key == "bangalore:delhi"
    assert record.status == "SUCCESS"
