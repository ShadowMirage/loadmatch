import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.responses import Response, coerce_response
from app.contracts.enums import Intent
from app.main import health_check
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.rating import Rating
from app.routers.debug import debug_dashboard
from app.services.ai_extraction_service import _normalize_parsed_result
from app.services.dispatcher_service import DispatcherService
from app.services.extraction_engine import ExtractionResult
from app.services.intent_resolver import IntentResolver
from app.routers.webhook import _extract_messages
from app.services.recovery_daemon import RecoveryDaemon
from app.services.recovery_service import RecoveryService
from app.services.supply_visibility_service import (
    _notify_shipper,
    _notify_subscriber_load,
    _notify_subscriber_truck,
    _notify_truck_owner,
)
from app.services.state_machine_service import StateMachineService
from app.models.enums import LoadRequestStatus
from app.services.whatsapp_service import (
    _check_and_set_response_sent,
    reset_response_guard,
    send_response,
    trigger_rating_requests,
)


class _FakeQuery:
    def __init__(self, model, storage):
        self.model = model
        self.storage = storage

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, value):
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


def _query_with_first(first_result):
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.first.return_value = first_result
    return query


def test_coerce_response_rehydrates_nested_contracts():
    response = coerce_response(
        {
            "text": "Choose",
            "buttons": [{"id": "CONFIRM_LOAD", "title": "Confirm"}],
            "sections": [
                {
                    "title": "Matches",
                    "rows": [{"id": "ROW1", "title": "Truck 1", "description": "Fast"}],
                }
            ],
            "list_button_text": "View",
        }
    )

    assert response.text == "Choose"
    assert response.buttons[0].id == "CONFIRM_LOAD"
    assert response.sections[0].rows[0].description == "Fast"
    assert response.list_button_text == "View"


def test_send_response_accepts_persisted_response_dict():
    captured = {}

    async def fake_send_text(to, text, ignore_guard=False, wa_id=None):
        captured["to"] = to
        captured["text"] = text
        captured["ignore_guard"] = ignore_guard
        captured["wa_id"] = wa_id

    async def run():
        with patch("app.services.whatsapp_service.send_text", fake_send_text):
            await send_response("919999999999", {"text": "Recovered"}, ignore_guard=True, wa_id="wamid.1")

    asyncio.run(run())

    assert captured == {
        "to": "919999999999",
        "text": "Recovered",
        "ignore_guard": True,
        "wa_id": "wamid.1",
    }


def test_process_local_response_guard_is_scoped_to_recipient_and_payload():
    reset_response_guard()
    try:
        assert _check_and_set_response_sent("919111111111", "text", {"text": "hello"}) is True
        assert _check_and_set_response_sent("919222222222", "text", {"text": "hello"}) is True
        assert _check_and_set_response_sent("919111111111", "text", {"text": "different"}) is True
        assert _check_and_set_response_sent("919111111111", "text", {"text": "hello"}) is False
    finally:
        reset_response_guard()


def test_send_with_backoff_returns_false_when_send_raises():
    async def fake_send_response(*args, **kwargs):
        raise RuntimeError("send failed")

    async def run():
        with patch("app.services.whatsapp_service.send_response", fake_send_response):
            service = RecoveryService(None)
            return await service.send_with_backoff(
                "919999999999",
                {"text": "Recovered"},
                retries=1,
                ignore_guard=True,
            )

    assert asyncio.run(run()) is False


def test_normalize_parsed_result_moves_top_level_fields_into_data():
    parsed = _normalize_parsed_result(
        {
            "action": "confirm_load_request",
            "from_city": "Jaipur",
            "to_city": "Delhi",
            "date": "tomorrow",
            "weight_kg": "5 ton",
            "cargo": "tiles",
        }
    )

    assert parsed["data"]["from_city"] == "Jaipur"
    assert parsed["data"]["to_city"] == "Delhi"
    assert parsed["data"]["weight_kg"] == 5000
    assert parsed["data"]["cargo"] == "tiles"
    assert parsed["data"]["date"]


def test_intent_resolver_keeps_partial_correction_inside_load_flow():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={"weight_kg": 5000},
        confidence=0.4,
        source="REGEX",
        trace_id="trace-1",
    )

    intent = resolver.resolve(extraction, interactive_payload=None, current_workflow="LOAD_FLOW")
    assert intent == Intent.CREATE_LOAD


def test_intent_resolver_maps_main_menu_buttons():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.1,
        source="REGEX",
        trace_id="trace-1",
    )

    assert resolver.resolve(extraction, {"id": "POST_TRUCK"}, None) == Intent.POST_TRUCK
    assert resolver.resolve(extraction, {"id": "FIND_TRUCK"}, None) == Intent.CREATE_LOAD
    assert resolver.resolve(extraction, {"id": "TRACK_BOOKING"}, None) == Intent.VIEW_LOADS
    assert resolver.resolve(extraction, {"id": "DELIVERY_STATUS"}, None) == Intent.VIEW_TRUCKS
    assert resolver.resolve(extraction, {"id": "UPLOAD_KYC"}, None) == Intent.UPLOAD_KYC


def test_intent_resolver_maps_rating_and_booking_actions():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.1,
        source="REGEX",
        trace_id="trace-1",
    )

    assert resolver.resolve(extraction, {"id": "RATING_match-1_5"}, None) == Intent.RATE_TRIP
    assert resolver.resolve(extraction, {"id": "TRACK_TRUCK_BKG123"}, None) == Intent.TRACK_TRUCK
    assert resolver.resolve(extraction, {"id": "CONTACT_DRIVER_BKG123"}, None) == Intent.CONTACT_DRIVER
    assert resolver.resolve(extraction, {"id": "CONFIRM_BOOKING_BKG123"}, None) == Intent.CONFIRM_BOOKING
    assert resolver.resolve(extraction, {"id": "CANCEL_BOOKING_BKG123"}, None) == Intent.CANCEL


def test_extract_messages_flattens_batched_webhook_payload():
    body = {
        "entry": [
            {
                "changes": [
                    {"value": {"messages": [{"id": "wamid.1"}, {"id": "wamid.2"}]}},
                    {"value": {"messages": [{"id": "wamid.3"}]}},
                ]
            }
        ]
    }

    assert [message["id"] for message in _extract_messages(body)] == [
        "wamid.1",
        "wamid.2",
        "wamid.3",
    ]


def test_dispatcher_confirm_load_uses_real_values_and_schema_fields():
    db = _FakeDB()
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "Jaipur",
            "to_city": "Delhi",
            "weight_kg": 5000,
            "date": "tomorrow",
            "budget_per_kg": 15,
        }
    )

    response = dispatcher._handle_confirm_load(payload)
    loads = db.storage.get(LoadRequest, [])

    assert "Load Created" in response.text
    assert loads
    assert loads[0].from_city == "Jaipur"
    assert loads[0].to_city == "Delhi"
    assert loads[0].weight_kg == 5000
    assert loads[0].budget_per_kg == 15
    assert isinstance(loads[0].pickup_date, date)


def test_dispatcher_confirm_load_surfaces_matching_summary_and_marks_load_matched():
    db = _FakeDB()
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "Jaipur",
            "to_city": "Delhi",
            "weight_kg": 5000,
            "date": "tomorrow",
            "budget_per_kg": 15,
        }
    )

    with patch(
        "app.services.dispatcher_service.find_matches_for_load_summary",
        return_value={
            "match_count": 2,
            "matches": [
                {"pickup": "Jaipur", "drop": "Delhi", "weight": 7000, "match_score": 96},
                {"pickup": "Ajmer", "drop": "Delhi", "weight": 6000, "match_score": 84},
            ],
        },
    ):
        response = dispatcher._handle_confirm_load(payload)

    loads = db.storage.get(LoadRequest, [])
    assert loads[0].status == LoadRequestStatus.matched
    assert "Found 2 matching truck(s):" in response.text
    assert "1. Jaipur → Delhi | 7000 kg | 96% match" in response.text


def test_dispatcher_confirm_truck_uses_real_values():
    db = _FakeDB()
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "Mumbai",
            "to_city": "Delhi",
            "capacity_kg": 7000,
            "date": "tomorrow",
            "rate_per_kg": 12,
            "plate": "MH12AB3456",
        }
    )

    response = dispatcher._handle_confirm_truck(payload)
    listings = db.storage.get(TruckSpaceListing, [])

    assert "Truck Posted" in response.text
    assert listings
    assert listings[0].from_city == "Mumbai"
    assert listings[0].to_city == "Delhi"
    assert listings[0].available_capacity_kg == 7000
    assert float(listings[0].price_per_kg) == 12.0
    assert isinstance(listings[0].departure_date, date)


def test_dispatcher_confirm_truck_surfaces_matching_summary():
    db = _FakeDB()
    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "Mumbai",
            "to_city": "Delhi",
            "capacity_kg": 7000,
            "date": "tomorrow",
            "rate_per_kg": 12,
            "plate": "MH12AB3456",
        }
    )

    with patch(
        "app.services.dispatcher_service.find_matches_for_truck_summary",
        return_value={
            "match_count": 1,
            "matches": [
                {"pickup": "Mumbai", "drop": "Delhi", "weight": 5000, "match_score": 91},
            ],
        },
    ):
        response = dispatcher._handle_confirm_truck(payload)

    assert "Found 1 matching load(s):" in response.text
    assert "1. Mumbai → Delhi | 5000 kg | 91% match" in response.text


def test_dispatcher_upload_kyc_prompt_is_actionable():
    db = _FakeDB()
    dispatcher = DispatcherService(db, user_id="user-123")

    response = dispatcher.execute(Intent.UPLOAD_KYC, payload={})

    assert "KYC Upload" in response.text
    assert response.buttons[0].id == "MAIN_MENU"


def test_dispatcher_rate_trip_saves_rating_from_interactive_action():
    match = SimpleNamespace(id="match-1", load_request_id="load-1", listing_id="listing-1")
    load = SimpleNamespace(id="load-1", shipper_id="user-123")
    listing = SimpleNamespace(id="listing-1", owner_id="user-456")
    db = MagicMock()
    db.query.side_effect = [
        _query_with_first(match),
        _query_with_first(load),
        _query_with_first(listing),
        _query_with_first(None),
    ]

    dispatcher = DispatcherService(db, user_id="user-123")
    payload = SimpleNamespace(data={"interactive_action_id": "RATING_match-1_5"})

    response = dispatcher.execute(Intent.RATE_TRIP, payload=payload)

    added_rating = db.add.call_args.args[0]
    assert isinstance(added_rating, Rating)
    assert added_rating.match_id == "match-1"
    assert added_rating.rater_id == "user-123"
    assert added_rating.rated_user_id == "user-456"
    assert added_rating.score == 5.0
    assert "5/5 rating has been saved" in response.text


def test_dispatcher_track_and_contact_driver_use_booking_code_actions():
    match = SimpleNamespace(id="match-1", booking_code="BKG123", listing_id="listing-1", status=SimpleNamespace(value="confirmed"), eta=None, driver_lat=19.07, driver_lng=72.87)
    listing = SimpleNamespace(id="listing-1", from_city="Mumbai", to_city="Delhi", owner=SimpleNamespace(phone="919111111111"))
    db = MagicMock()
    db.query.side_effect = [
        _query_with_first(match),
        _query_with_first(listing),
        _query_with_first(match),
        _query_with_first(listing),
    ]
    dispatcher = DispatcherService(db, user_id="user-123")

    tracking = dispatcher.execute(Intent.TRACK_TRUCK, payload=SimpleNamespace(data={"interactive_action_id": "TRACK_TRUCK_BKG123"}))
    contact = dispatcher.execute(Intent.CONTACT_DRIVER, payload=SimpleNamespace(data={"interactive_action_id": "CONTACT_DRIVER_BKG123"}))

    assert "Booking BKG123" in tracking.text
    assert "Status: confirmed" in tracking.text
    assert "Live location: 19.07, 72.87" in tracking.text
    assert "Driver contact: 919111111111" in contact.text


def test_dispatcher_confirm_booking_returns_booking_status():
    match = SimpleNamespace(booking_code="BKG123", status=SimpleNamespace(value="pending"))
    db = MagicMock()
    db.query.return_value = _query_with_first(match)
    dispatcher = DispatcherService(db, user_id="user-123", phone="919999999999")

    response = dispatcher.execute(Intent.CONFIRM_BOOKING, payload=SimpleNamespace(data={"interactive_action_id": "CONFIRM_BOOKING_BKG123"}))

    assert "Booking BKG123" in response.text
    assert "Current status: pending" in response.text


def test_supply_visibility_buttons_align_with_supported_intents():
    captured = []

    async def fake_send_interactive_buttons(phone, body, buttons, **kwargs):
        captured.append((phone, buttons))

    load = SimpleNamespace(from_city="Jaipur", to_city="Delhi", weight_kg=5000, category=None)
    listing = SimpleNamespace(from_city="Mumbai", to_city="Delhi", available_capacity_kg=7000, price_per_kg=12)
    user = SimpleNamespace(phone="919999999999")

    async def run():
        with patch("app.services.supply_visibility_service.send_interactive_buttons", fake_send_interactive_buttons):
            await _notify_truck_owner(user, load)
            await _notify_shipper(user, listing)
            await _notify_subscriber_load(user, load)
            await _notify_subscriber_truck(user, listing)

    asyncio.run(run())

    assert captured[0][1] == [{"id": "POST_TRUCK", "title": "Post Truck"}]
    assert captured[1][1] == [{"id": "POST_LOAD", "title": "📦 Post Load"}]
    assert captured[2][1] == [
        {"id": "POST_TRUCK", "title": "🚚 Post Truck"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"},
    ]
    assert captured[3][1] == [
        {"id": "POST_LOAD", "title": "📦 Post Load"},
        {"id": "MAIN_MENU", "title": "🏠 Main Menu"},
    ]


def test_trigger_rating_requests_embeds_match_id_in_rating_buttons():
    match = SimpleNamespace(id="match-1", load_request_id="load-1", listing_id="listing-1")
    load = SimpleNamespace(id="load-1", shipper_id="shipper-1")
    listing = SimpleNamespace(id="listing-1", owner_id="transporter-1")
    shipper = SimpleNamespace(id="shipper-1", phone="919111111111")
    transporter = SimpleNamespace(id="transporter-1", phone="919222222222")
    rating_query = _query_with_first(None)
    db = MagicMock()
    db.query.side_effect = [
        _query_with_first(match),
        _query_with_first(load),
        _query_with_first(listing),
        _query_with_first(shipper),
        _query_with_first(transporter),
        rating_query,
        rating_query,
    ]
    captured = []

    async def fake_send_interactive_list(phone, text, button_text, sections, **kwargs):
        captured.append((phone, sections))

    async def run():
        with patch("app.services.whatsapp_service.send_interactive_list", fake_send_interactive_list), \
             patch("app.services.session_manager.update_session", return_value=None):
            await trigger_rating_requests(db, "match-1")

    asyncio.run(run())

    assert captured
    assert len(captured) == 2
    assert captured[0][1][0]["rows"][0]["id"] == "RATING_match-1_5"


def test_dispatcher_view_loads_returns_recent_rows():
    db = _FakeDB()
    db.storage[LoadRequest] = [
        SimpleNamespace(
            id="load-1",
            shipper_id="user-123",
            from_city="Jaipur",
            to_city="Delhi",
            pickup_date=date.today(),
            weight_kg=5000,
            status=SimpleNamespace(value="open"),
            created_at=date.today(),
        )
    ]
    dispatcher = DispatcherService(db, user_id="user-123", phone="919999999999")

    response = dispatcher.execute(Intent.VIEW_LOADS, payload={})

    assert response.sections
    assert response.sections[0].rows[0].title == "Jaipur → Delhi"


def test_state_machine_handles_first_message_without_timestamp():
    machine = StateMachineService()
    transition = machine.transition("IDLE", Intent.CREATE_LOAD, last_updated=None)

    assert transition.allowed is True
    assert transition.next_state == "LOAD_FLOW"


def test_health_check_stays_local_and_reports_configured_provider():
    class FakeSessionContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query):
            return 1

    class FakeRedis:
        def ping(self):
            return True

    async def run():
        with patch("app.main.SessionLocal", return_value=FakeSessionContext()), \
             patch("app.main.environment.redis_enabled", return_value=True), \
             patch("app.main.get_redis_client", return_value=FakeRedis()), \
             patch("app.main.environment.anthropic_enabled", return_value=True):
            return await health_check()

    result = asyncio.run(run())

    assert result["status"] == "ok"
    assert result["db"] == "ok"
    assert result["redis"] == "ok"
    assert result["anthropic"] == "configured"


def test_debug_dashboard_reads_event_data_field():
    debug_logs = [
        SimpleNamespace(
            created_at=datetime(2026, 1, 1, 9, 0, 0),
            event_type="DEBUG_AI_METRIC",
            user_id="user-1",
            data={"val_status": "action_extracted"},
        )
    ]
    metric_logs = debug_logs + [
        SimpleNamespace(
            created_at=datetime(2026, 1, 1, 10, 0, 0),
            event_type="DEBUG_AI_METRIC",
            user_id="user-2",
            data={"val_status": "needs_review"},
        )
    ]

    recent_query = MagicMock()
    recent_query.filter.return_value = recent_query
    recent_query.order_by.return_value = recent_query
    recent_query.limit.return_value = recent_query
    recent_query.all.return_value = debug_logs

    metric_query = MagicMock()
    metric_query.filter.return_value = metric_query
    metric_query.all.return_value = metric_logs

    db = MagicMock()
    db.query.side_effect = [recent_query, metric_query]

    result = debug_dashboard(db)

    assert result["system_health"]["success_rate"] == "50.0%"
    assert result["recent_logs"][0]["data"] == {"val_status": "action_extracted"}
    assert result["recent_logs"][0]["payload"] == {"val_status": "action_extracted"}


def test_replay_record_passes_workflow_and_phone_to_dispatcher():
    captured = {}
    record = SimpleNamespace(
        id="pm-1",
        idempotency_key="user-123:CONFIRM:wamid.1:IDLE",
        trace_id="trace-1",
        request_payload={
            "intent": "CONFIRM",
            "payload": {"action": "CONFIRM", "data": {}},
            "phone": "919999999999",
            "current_workflow": "LOAD_FLOW",
        },
        status="EXECUTING",
        retry_count=0,
        execution_owner=None,
        error_log=None,
        response_payload=None,
    )

    def fake_execute(self, intent, payload, current_workflow=None):
        captured["intent"] = getattr(intent, "value", str(intent))
        captured["payload_type"] = type(payload).__name__
        captured["workflow"] = current_workflow
        captured["phone"] = self.phone
        return Response(text="ok")

    async def run():
        daemon = RecoveryDaemon(lambda: None)
        with patch("app.services.dispatcher_service.DispatcherService.execute", fake_execute):
            await daemon._replay_record(_FakeDB(), record)

    asyncio.run(run())

    assert captured == {
        "intent": "CONFIRM",
        "payload_type": "GenericActionPayload",
        "workflow": "LOAD_FLOW",
        "phone": "919999999999",
    }
