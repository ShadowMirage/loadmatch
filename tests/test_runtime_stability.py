import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.responses import Response, coerce_response
from app.contracts.enums import Intent
from app.contracts.route_confidence import RouteConfidence
from app.main import backfill_status, constraint_drift, health_check, liquidity_health
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.rating import Rating
from app.routers.debug import debug_dashboard
from app.services.ai_extraction_service import _normalize_parsed_result
from app.services.dispatcher_service import DispatcherService
from app.services.extraction_engine import ExtractionResult
from app.services.intent_resolver import IntentResolver
from app.services.logistics_data import RESOLVER_VERSION
from app.routers.webhook import _extract_messages, _phase2_atomic_dispatch
from app.services.recovery_daemon import RecoveryDaemon
from app.services.recovery_service import DeliveryResult, RecoveryService
from app.services.idempotency_service import IdempotencyService
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

    assert asyncio.run(run()) == DeliveryResult.FAILED


def test_send_with_backoff_skips_sandbox_block_without_retry_loop():
    attempts = {"count": 0}

    async def fake_send_response(*args, **kwargs):
        attempts["count"] += 1
        raise RuntimeError("(#131030) Recipient phone number not in allowed list")

    async def run():
        with patch("app.services.whatsapp_service.send_response", fake_send_response):
            service = RecoveryService(None)
            return await service.send_with_backoff(
                "919999999999",
                {"text": "Recovered"},
                retries=5,
                ignore_guard=True,
            )

    assert asyncio.run(run()) == DeliveryResult.SKIPPED_SANDBOX
    assert attempts["count"] == 1


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


def test_truck_flow_corridor_enrichment_without_intent_flip():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.0,
        source="TEST",
        trace_id="trace-truck-flow",
    )

    intent = resolver.resolve(
        extraction,
        interactive_payload=None,
        current_workflow="TRUCK_FLOW",
        message_text="delhi to jaipur",
    )

    assert intent == Intent.POST_TRUCK
    assert extraction.data["from_city"] == "delhi"
    assert extraction.data["to_city"] == "jaipur"
    assert extraction.data["lane_key"] == "delhi:jaipur"
    assert extraction.data["corridor_detected"] is True


def test_explicit_load_intent_overrides_truck_flow_corridor_anchor():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.0,
        source="TEST",
        trace_id="trace-load-override",
    )

    intent = resolver.resolve(
        extraction,
        interactive_payload=None,
        current_workflow="TRUCK_FLOW",
        message_text="post load delhi to jaipur",
    )

    assert intent == Intent.CREATE_LOAD
    assert extraction.data["lane_key"] == "delhi:jaipur"
    assert extraction.data["corridor_detected"] is True


def test_explicit_truck_intent_overrides_load_flow_corridor_anchor():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.0,
        source="TEST",
        trace_id="trace-truck-override",
    )

    intent = resolver.resolve(
        extraction,
        interactive_payload=None,
        current_workflow="LOAD_FLOW",
        message_text="need truck delhi to jaipur",
    )

    assert intent == Intent.POST_TRUCK
    assert extraction.data["lane_key"] == "delhi:jaipur"
    assert extraction.data["corridor_detected"] is True


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


def test_dispatcher_skips_route_questions_for_high_confidence_load_lane():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")
    payload = SimpleNamespace(
        data={
            "from_city": "delhi",
            "to_city": "jaipur",
            "confidence_source": "corridor_detection",
            "corridor_source": "alias_pair",
        }
    )

    response = dispatcher.execute(Intent.CREATE_LOAD, payload=payload)

    assert "1. Load weight" in response.text


def test_route_confidence_class_high_city_pair():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")

    assert dispatcher._route_confidence_class(
        {
            "confidence_source": "corridor_detection",
            "corridor_source": "city_pair",
        }
    ) == RouteConfidence.HIGH


def test_route_confidence_class_high_alias_pair():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")

    assert dispatcher._route_confidence_class(
        {
            "confidence_source": "corridor_detection",
            "corridor_source": "alias_pair",
        }
    ) == RouteConfidence.HIGH


def test_route_confidence_class_medium_adjacent_pair():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")

    assert dispatcher._route_confidence_class(
        {
            "confidence_source": "corridor_detection",
            "corridor_source": "adjacent_city_pair",
        }
    ) == RouteConfidence.MEDIUM


def test_route_confidence_class_medium_llm_structured():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")

    assert dispatcher._route_confidence_class(
        {
            "confidence_source": "llm_structured",
        }
    ) == RouteConfidence.MEDIUM


def test_route_confidence_class_low_regex():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")

    assert dispatcher._route_confidence_class(
        {
            "confidence_source": "regex",
        }
    ) == RouteConfidence.LOW


def test_route_confidence_class_missing_confidence_source_warns_and_falls_back_low():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")

    with patch("app.services.dispatcher_service.logger.warning") as mock_warning:
        confidence = dispatcher._route_confidence_class(
            {
                "lane_key": "delhi:jaipur",
                "directional_lane_key": "delhi->jaipur",
            }
        )

    assert confidence == RouteConfidence.LOW
    warning_messages = [call.args[0] for call in mock_warning.call_args_list]
    assert "[MISSING_RESOLVER_VERSION]" in warning_messages
    assert "Dispatcher pacing fallback: extraction_data missing confidence_source" in warning_messages


def test_dispatcher_confirms_adjacent_load_route_once_before_next_slot():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123", phone="919999999999")
    payload = SimpleNamespace(
        data={
            "from_city": "delhi",
            "to_city": "jaipur",
            "confidence_source": "corridor_detection",
            "corridor_source": "adjacent_city_pair",
        }
    )

    with patch("app.services.dispatcher_service.get_session_data", return_value={}), \
         patch("app.services.dispatcher_service.set_session_data") as mock_set_session_data:
        response = dispatcher.execute(Intent.CREATE_LOAD, payload=payload)

    assert "Just confirming the route: Delhi → Jaipur." in response.text
    assert "load weight" in response.text.lower()
    mock_set_session_data.assert_called_once_with(
        dispatcher.db,
        "919999999999",
        "user-123",
        {"route_confirmation_prompted_for": "delhi->jaipur"},
    )


def test_dispatcher_does_not_repeat_route_confirmation_after_prompting_once():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123", phone="919999999999")
    payload = SimpleNamespace(
        data={
            "from_city": "delhi",
            "to_city": "jaipur",
            "confidence_source": "corridor_detection",
            "corridor_source": "adjacent_city_pair",
        }
    )

    with patch(
        "app.services.dispatcher_service.get_session_data",
        return_value={"route_confirmation_prompted_for": "delhi->jaipur"},
    ), patch("app.services.dispatcher_service.set_session_data") as mock_set_session_data:
        response = dispatcher.execute(Intent.CREATE_LOAD, payload=payload)

    assert "1. Load weight" in response.text
    mock_set_session_data.assert_not_called()


def test_dispatcher_confirms_llm_structured_truck_route_before_capacity():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123", phone="919999999999")
    payload = SimpleNamespace(
        data={
            "current_city": "bangalore",
            "to_city": "delhi",
            "confidence_source": "llm_structured",
        }
    )

    with patch("app.services.dispatcher_service.get_session_data", return_value={}), \
         patch("app.services.dispatcher_service.set_session_data") as mock_set_session_data:
        response = dispatcher.execute(Intent.POST_TRUCK, payload=payload)

    assert "Just confirming the route: Bangalore → Delhi." in response.text
    assert "truck capacity" in response.text.lower()
    mock_set_session_data.assert_called_once_with(
        dispatcher.db,
        "919999999999",
        "user-123",
        {"route_confirmation_prompted_for": "bangalore->delhi"},
    )


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
    assert result["canonical_lane_key_backfill"] == {
        "pending_count": 1,
        "total_count": 1,
        "progress_ratio": 0.0,
    }


def test_backfill_status_reports_remaining_rows():
    class FakeSessionContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query):
            text_query = str(query)
            if "WHERE canonical_lane_key IS NULL" in text_query:
                return 2
            if "FROM truck_space_listings" in text_query:
                return 5
            return 1

    async def run():
        with patch("app.main.SessionLocal", return_value=FakeSessionContext()):
            return await backfill_status()

    result = asyncio.run(run())

    assert result == {
        "canonical_lane_backfill_complete": False,
        "remaining_rows": 2,
        "total_rows": 5,
        "progress_ratio": 0.6,
    }


def test_liquidity_health_returns_internal_snapshot():
    class FakeSessionContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    async def run():
        with patch("app.main.SessionLocal", return_value=FakeSessionContext()), patch(
            "app.main.InternalMonitoringService.get_liquidity_health_snapshot",
            return_value={
                "active_lanes": 12,
                "vehicle_segments": 4,
                "avg_lane_supply": 2.1,
                "avg_lane_demand": 1.3,
                "imbalance_ratio": 0.62,
                "freshness_suppression_rate": 0.14,
                "duplicate_load_reuse_rate": 0.19,
                "duplicate_listing_reuse_rate": 0.09,
            },
        ):
            return await liquidity_health()

    result = asyncio.run(run())

    assert result == {
        "status": "ok",
        "active_lanes": 12,
        "vehicle_segments": 4,
        "avg_lane_supply": 2.1,
        "avg_lane_demand": 1.3,
        "imbalance_ratio": 0.62,
        "freshness_suppression_rate": 0.14,
        "duplicate_load_reuse_rate": 0.19,
        "duplicate_listing_reuse_rate": 0.09,
    }


def test_constraint_drift_returns_aggregated_surface():
    class FakeSessionContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    async def run():
        with patch("app.main.SessionLocal", return_value=FakeSessionContext()), patch(
            "app.main.InternalMonitoringService.get_constraint_drift_metrics",
            return_value={
                "checked_at": "2026-04-08T00:00:00+00:00",
                "listing_canonical_lane_key_null_count": 1,
                "listing_vehicle_type_null_count": 2,
                "load_canonical_lane_key_null_count": 3,
                "load_vehicle_type_null_count": 4,
                "duplicate_active_listing_groups": 1,
                "duplicate_active_load_groups": 2,
                "stale_executing_count": 5,
                "request_payload_null_count": 6,
                "total_violations": 24,
            },
        ):
            return await constraint_drift()

    result = asyncio.run(run())

    assert result["status"] == "drift_detected"
    assert result["canonical_lane_key_null_rows"] == 4
    assert result["vehicle_type_null_rows"] == 6
    assert result["duplicate_active_listings"] == 1
    assert result["duplicate_active_loads"] == 2
    assert result["stale_executing_messages"] == 5
    assert result["request_payload_null_rows"] == 6
    assert result["total_violations"] == 24


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


def test_replay_record_prefers_preserved_extraction_data_for_reconstruction():
    captured = {}
    record = SimpleNamespace(
        id="pm-2",
        idempotency_key="user-123:CONFIRM:wamid.2:LOAD_FLOW",
        trace_id="trace-2",
        request_payload={
            "intent": "CONFIRM",
            "payload": {"from_city": "delhi", "to_city": "jaipur"},
            "extraction_data": {
                "from_city": "delhi",
                "to_city": "jaipur",
                "weight_kg": 5000,
                "lane_key": "delhi:jaipur",
                "directional_lane_key": "delhi->jaipur",
                "resolver_version": RESOLVER_VERSION,
            },
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
        captured["lane_key"] = payload.data.get("lane_key")
        captured["resolver_version"] = payload.data.get("resolver_version")
        captured["weight_kg"] = payload.data.get("weight_kg")
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
        "lane_key": "delhi:jaipur",
        "resolver_version": RESOLVER_VERSION,
        "weight_kg": 5000,
    }


def test_recovery_takeover_sets_executing_state():
    record = SimpleNamespace(
        id="pm-takeover",
        idempotency_key="user-123:CONFIRM:wamid.takeover:LOAD_FLOW",
        trace_id="trace-takeover",
        request_payload={"current_workflow": "LOAD_FLOW"},
        workflow_step="LOAD_FLOW",
        retry_count=0,
        status="FAILED",
        recovery_attempted_at=None,
        delivery_state="PENDING",
    )
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.limit.return_value = query
    query.all.return_value = [record]
    db = MagicMock()
    db.query.return_value = query

    async def run():
        daemon = RecoveryDaemon(lambda: db)
        with patch("app.services.recovery_daemon.logger.info") as mock_info, \
             patch.object(daemon, "_replay_record") as mock_replay_record:
            await daemon.scan_and_replay()
            return mock_info, mock_replay_record

    mock_info, mock_replay_record = asyncio.run(run())

    info_messages = [call.args[0] for call in mock_info.call_args_list]
    assert "RECOVERY_TAKEOVER_EXECUTING_MESSAGE" in info_messages
    assert record.delivery_state == "EXECUTING"
    mock_replay_record.assert_awaited_once_with(db, record)


def test_recovery_delivery_marks_sandbox_block_as_terminal():
    record = SimpleNamespace(
        id="pm-sandbox",
        idempotency_key="user-123:POST_TRUCK:wamid.sandbox:TRUCK_FLOW",
        trace_id="trace-sandbox",
        request_payload={"phone": "919999999999"},
        response_payload={"text": "Truck Posted"},
        status="SUCCESS",
        delivered_at=None,
        delivery_state=None,
        failed_at=None,
        error_log=None,
    )
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.limit.return_value = query
    query.all.return_value = [record]
    db = MagicMock()
    db.query.return_value = query

    async def run():
        daemon = RecoveryDaemon(lambda: db)
        with patch("app.services.recovery_daemon.RecoveryService.send_with_backoff", return_value=DeliveryResult.SKIPPED_SANDBOX):
            await daemon.scan_and_deliver()

    asyncio.run(run())

    assert record.delivery_state == "SKIPPED_SANDBOX"
    assert record.failed_at is not None
    assert record.error_log["code"] == 131030
    db.commit.assert_called()


def test_state_machine_does_not_log_idle_expiration_noise():
    service = StateMachineService()
    stale_time = datetime(2025, 1, 1)

    with patch("app.services.state_machine_service.logger.info") as mock_info:
        result = service.transition("IDLE", Intent.UNKNOWN, stale_time)

    assert result.next_state == "IDLE"
    info_messages = [call.args[0] for call in mock_info.call_args_list]
    assert "Session expired (State: IDLE). Reverting to IDLE." not in info_messages


def test_fetch_cached_intent_data_ignores_inflight_records():
    service = IdempotencyService(db=None)
    service.find_record = MagicMock(return_value=SimpleNamespace(
        status="IN_PROGRESS",
        intent="CREATE_LOAD",
        request_payload={"extraction_data": {"lane_key": "delhi:jaipur"}},
    ))

    assert service.fetch_cached_intent_data("wamid.1") is None


def test_fetch_cached_intent_data_uses_success_records_only():
    service = IdempotencyService(db=None)
    service.find_record = MagicMock(return_value=SimpleNamespace(
        status="SUCCESS",
        intent="CREATE_LOAD",
        request_payload={"extraction_data": {"lane_key": "delhi:jaipur"}},
    ))

    assert service.fetch_cached_intent_data("wamid.1") == (
        Intent.CREATE_LOAD,
        {"lane_key": "delhi:jaipur"},
    )


def test_idempotency_start_suppresses_existing_executing_state():
    stale_time = datetime(2026, 1, 1)
    existing = SimpleNamespace(
        status="EXECUTING",
        delivery_state="EXECUTING",
        updated_at=stale_time,
        created_at=stale_time,
    )
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.first.return_value = existing
    db = MagicMock()
    db.query.return_value = query
    service = IdempotencyService(db=db)

    result = service.start("idem-key", "trace-1", request_payload={"intent": "CONFIRM"})

    assert result is None
    db.flush.assert_not_called()


def test_executing_overlap_marker_logged():
    stale_time = datetime(2026, 1, 1)
    existing = SimpleNamespace(
        status="EXECUTING",
        delivery_state="EXECUTING",
        updated_at=stale_time,
        created_at=stale_time,
    )
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.first.return_value = existing
    db = MagicMock()
    db.query.return_value = query
    service = IdempotencyService(db=db)

    with patch("app.services.idempotency_service.logger.info") as mock_info:
        result = service.start("idem-key", "trace-1", request_payload={"intent": "CONFIRM"}, wamid="wamid.1")

    assert result is None
    mock_info.assert_called_once()
    assert mock_info.call_args.args[0] == "EXECUTING_OVERLAP_SUPPRESSED"
    assert mock_info.call_args.kwargs["extra"] == {
        "wamid": "wamid.1",
        "trace_id": "trace-1",
    }


def test_scan_and_reclaim_clears_executing_delivery_state():
    record = SimpleNamespace(
        idempotency_key="user-123:CONFIRM:wamid.reclaim:LOAD_FLOW",
        execution_owner="worker-1",
        status="EXECUTING",
        delivery_state="EXECUTING",
        execution_started_at=datetime(2026, 1, 1),
    )
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.all.return_value = [record]
    db = MagicMock()
    db.query.return_value = query

    async def run():
        daemon = RecoveryDaemon(lambda: db)
        await daemon.scan_and_reclaim()

    asyncio.run(run())

    assert record.status == "IN_PROGRESS"
    assert record.delivery_state == "PENDING"
    assert record.execution_owner is None
    assert record.execution_started_at is None


def test_zombie_reset_allows_reexecution():
    stale_time = datetime(2026, 1, 1)
    existing = SimpleNamespace(
        status="IN_PROGRESS",
        delivery_state="PENDING",
        updated_at=stale_time,
        created_at=stale_time,
        trace_id="old-trace",
        request_payload={"intent": "OLD"},
        retry_count=1,
        user_id="user-123",
        wamid="wamid.old",
        intent="OLD",
        confidence=10,
        workflow_step="LOAD_FLOW",
        dispatcher_action="OLD",
        dispatch_started_at=stale_time,
    )
    query = MagicMock()
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.first.return_value = existing
    db = MagicMock()
    db.query.return_value = query
    service = IdempotencyService(db=db, ttl_seconds=30)

    result = service.start(
        "idem-key",
        "trace-new",
        request_payload={"intent": "CONFIRM"},
        wamid="wamid.new",
        user_id="user-456",
        intent="CONFIRM",
        confidence=99,
        workflow_step="CONFIRM_FLOW",
        dispatcher_action="CONFIRM",
        delivery_state="PENDING",
    )

    assert result is existing
    assert existing.status == "IN_PROGRESS"
    assert existing.trace_id == "trace-new"
    assert existing.request_payload == {"intent": "CONFIRM"}
    assert existing.retry_count == 2
    assert existing.user_id == "user-456"
    assert existing.wamid == "wamid.new"
    assert existing.intent == "CONFIRM"
    assert existing.confidence == 99
    assert existing.workflow_step == "CONFIRM_FLOW"
    assert existing.dispatcher_action == "CONFIRM"
    assert existing.delivery_state == "PENDING"


def test_replay_lane_key_stability():
    resolver = IntentResolver()
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.0,
        source="TEST",
        trace_id="trace-lane-key",
    )
    resolver.resolve(
        extraction,
        interactive_payload=None,
        current_workflow="IDLE",
        message_text="blr to delhi",
    )

    stored_lane_key = extraction.data["lane_key"]
    captured = {}
    record = SimpleNamespace(
        id="pm-3",
        idempotency_key="user-123:CONFIRM:wamid.3:LOAD_FLOW",
        trace_id="trace-3",
        request_payload={
            "intent": "CONFIRM",
            "payload": {"from_city": "bangalore", "to_city": "delhi"},
            "extraction_data": dict(extraction.data),
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
        captured["lane_key"] = payload.data.get("lane_key")
        captured["from_city"] = payload.data.get("from_city")
        captured["resolver_version"] = payload.data.get("resolver_version")
        return Response(text="ok")

    async def run():
        daemon = RecoveryDaemon(lambda: None)
        with patch("app.services.dispatcher_service.DispatcherService.execute", fake_execute):
            await daemon._replay_record(_FakeDB(), record)

    asyncio.run(run())

    assert stored_lane_key == "bangalore:delhi"
    assert captured == {
        "lane_key": stored_lane_key,
        "from_city": "bangalore",
        "resolver_version": RESOLVER_VERSION,
    }
    # User refinement: verify directional equality in replay
    assert record.request_payload["extraction_data"]["directional_lane_key"] == "bangalore->delhi"

def test_route_confidence_enum_enforcement():
    """Ensures RouteConfidence remains a strict enum and is correctly classed."""
    from app.contracts.route_confidence import RouteConfidence
    assert isinstance(RouteConfidence.HIGH, RouteConfidence)
    assert RouteConfidence.HIGH.value == "HIGH"


def test_workflow_taxonomy_helpers_are_stable():
    assert StateMachineService.workflow_is_active(None) is False
    assert StateMachineService.workflow_is_active("") is False
    assert StateMachineService.workflow_is_active("LOAD_FLOW") is True
    assert StateMachineService.workflow_is_active("LOAD_CONFIRM") is True
    assert StateMachineService.workflow_is_confirm_stage("LOAD_CONFIRM") is True
    assert StateMachineService.workflow_is_confirm_stage("LOAD_FLOW") is False
    assert StateMachineService.workflow_is_confirm_stage(None) is False
    assert StateMachineService.workflow_is_terminal("SUCCESS") is True
    assert StateMachineService.workflow_is_terminal("CANCELLED") is True
    assert StateMachineService.workflow_is_terminal("FAILED") is True
    assert StateMachineService.workflow_is_terminal("IDLE") is False
    assert StateMachineService.workflow_is_terminal("TRUCK_FLOW") is False


def test_active_workflow_requires_complete_session_data():
    session_store = {
        "lane_key": None,
        "directional_lane_key": None,
        "from_city": None,
        "to_city": None,
    }

    workflow = StateMachineService.reconstruct_workflow("LOAD_FLOW", session_store)

    assert workflow is None


def test_slot_only_reconstruction_keeps_active_workflow():
    session_store = {
        "weight": "7 ton",
        "date": "tomorrow",
        "lane_key": None,
        "directional_lane_key": None,
        "from_city": None,
        "to_city": None,
    }

    workflow = StateMachineService.reconstruct_workflow("LOAD_FLOW", session_store)

    assert workflow == "LOAD_FLOW"


def test_slot_only_reconstruction_does_not_emit_abort_marker(caplog):
    session_store = {
        "weight": "7 ton",
        "date": "tomorrow",
    }

    StateMachineService.reconstruct_workflow("LOAD_FLOW", session_store)

    assert not any(
        "[SESSION_RECONSTRUCTION_ABORT]" in record.message
        for record in caplog.records
    )


def test_terminal_cleanup_clears_provenance_metadata():
    session_store = {
        "lane_key": "delhi:jaipur",
        "directional_lane_key": "delhi->jaipur",
        "reverse_directional_lane_key": "jaipur->delhi",
        "lane_class": "regional_lane",
        "corridor_detected": True,
        "confidence_source": "corridor_detection",
        "corridor_source": "city_pair",
        "resolver_version": "v3",
    }

    cleaned = StateMachineService.cleanup_terminal_state(session_store)

    assert cleaned == {}
    assert session_store == {}


def test_phase2_suppresses_secondary_response_for_inflight_duplicate():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="LOAD_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="LOAD_FLOW", error_message=None)
    idempotency = MagicMock()
    idempotency.start.return_value = None
    idempotency.fetch_cached_response.return_value = None
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CREATE_LOAD,
        data={"from_city": "delhi", "to_city": "jaipur"},
        confidence=1.0,
        source="TEST",
        trace_id="trace-dup",
    )

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_or_create_session", return_value=None), \
             patch("app.routers.webhook.set_session_data", return_value=None), \
             patch("app.routers.webhook.update_session", return_value=None):
            return await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.dup",
                db=db,
                trace_id="trace-dup",
                intent=Intent.CREATE_LOAD,
                payload={"from_city": "delhi", "to_city": "jaipur"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )

    response, idem_key, msg_id = asyncio.run(run())

    assert response is None
    assert idem_key == "user-123:CREATE_LOAD:wamid.dup:LOAD_FLOW"
    assert msg_id is None


def test_phase2_clears_session_context_when_cancel_returns_to_idle():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="TRUCK_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="IDLE", error_message=None)
    idempotency = MagicMock()
    idempotency.start.return_value = SimpleNamespace(id="pm-cancel")
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CANCEL,
        data={
            "current_city": "jaipur",
            "to_city": "gwalior",
            "capacity_kg": 7000,
        },
        confidence=1.0,
        source="TEST",
        trace_id="trace-cancel",
    )

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.clear_session") as mock_clear_session, \
             patch("app.routers.webhook.get_or_create_session") as mock_get_or_create_session, \
             patch("app.routers.webhook.set_session_data") as mock_set_session_data, \
             patch("app.routers.webhook.update_session") as mock_update_session:
            response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.cancel",
                db=db,
                trace_id="trace-cancel",
                intent=Intent.CANCEL,
                payload={"action": "CANCEL"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )
            return response, mock_clear_session, mock_get_or_create_session, mock_set_session_data, mock_update_session

    response, mock_clear_session, mock_get_or_create_session, mock_set_session_data, mock_update_session = asyncio.run(run())

    assert "cancelled" in response.text.lower()
    mock_clear_session.assert_called_once_with(db, "919999999999")
    mock_get_or_create_session.assert_not_called()
    mock_set_session_data.assert_not_called()
    mock_update_session.assert_not_called()


def test_phase2_clears_session_context_when_confirm_returns_to_idle():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="LOAD_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="IDLE", error_message=None)
    idempotency = MagicMock()
    idempotency.start.return_value = SimpleNamespace(id="pm-confirm")
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CONFIRM,
        data={
            "from_city": "jaipur",
            "to_city": "gwalior",
            "weight_kg": 7000,
        },
        confidence=1.0,
        source="TEST",
        trace_id="trace-confirm",
    )

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.clear_session") as mock_clear_session, \
             patch("app.routers.webhook.set_session_data") as mock_set_session_data, \
             patch("app.routers.webhook.DispatcherService.execute", return_value=Response(text="Load Created")):
            response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.confirm",
                db=db,
                trace_id="trace-confirm",
                intent=Intent.CONFIRM,
                payload={"action": "CONFIRM"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )
            return response, mock_clear_session, mock_set_session_data

    response, mock_clear_session, mock_set_session_data = asyncio.run(run())

    assert response.text == "Load Created"
    mock_clear_session.assert_called_once_with(db, "919999999999")
    mock_set_session_data.assert_not_called()


def test_phase2_preserves_slots_when_workflow_expires():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="LOAD_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="IDLE", error_message=None, workflow_expired=True)
    idempotency = MagicMock()
    idempotency.start.return_value = SimpleNamespace(id="pm-expired")
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={
            "from_city": "chennai",
            "to_city": "mumbai",
            "weight_kg": 10000,
        },
        confidence=1.0,
        source="TEST",
        trace_id="trace-expired",
    )

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_session_data", return_value={"from_city": "chennai", "weight_kg": 8000}), \
             patch("app.routers.webhook.get_or_create_session") as mock_get_or_create_session, \
             patch("app.routers.webhook.set_session_data") as mock_set_session_data, \
             patch("app.routers.webhook.update_session") as mock_update_session, \
             patch("app.routers.webhook.clear_session") as mock_clear_session, \
             patch("app.routers.webhook.DispatcherService.execute", return_value=Response(text="prompt")):
            response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.expired",
                db=db,
                trace_id="trace-expired",
                intent=Intent.UNKNOWN,
                payload={"action": "UNKNOWN"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )
            return response, mock_get_or_create_session, mock_set_session_data, mock_update_session, mock_clear_session

    response, mock_get_or_create_session, mock_set_session_data, mock_update_session, mock_clear_session = asyncio.run(run())

    assert response.text == "prompt"
    mock_get_or_create_session.assert_called_once_with(db, "919999999999", "user-123")
    mock_set_session_data.assert_called_once()
    mock_update_session.assert_called_once_with(
        db,
        "919999999999",
        {"current_workflow": None},
        commit=False,
    )
    mock_clear_session.assert_not_called()


def test_phase2_cleans_inactive_workflow_with_lane_key_without_warning():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="TRUCK_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="IDLE", error_message=None)
    idempotency = MagicMock()
    idempotency.start.return_value = SimpleNamespace(id="pm-cancel-warning")
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CANCEL,
        data={},
        confidence=1.0,
        source="TEST",
        trace_id="trace-cancel-warning",
    )

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_session_data", return_value={"current_city": "jaipur", "lane_key": "gwalior:jaipur"}), \
             patch("app.routers.webhook.clear_session") as mock_clear_session, \
             patch("app.routers.webhook.logger.warning") as mock_warning, \
             patch("app.services.state_machine_service.logger.warning") as mock_state_warning:
            response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.cancel.warning",
                db=db,
                trace_id="trace-cancel-warning",
                intent=Intent.CANCEL,
                payload={"action": "CANCEL"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )
            return response, mock_clear_session, mock_warning, mock_state_warning

    response, mock_clear_session, mock_warning, mock_state_warning = asyncio.run(run())

    assert "cancelled" in response.text.lower()
    mock_clear_session.assert_called_once_with(db, "919999999999")
    mock_warning.assert_not_called()
    mock_state_warning.assert_not_called()


def test_phase2_does_not_warn_for_inactive_workflow_without_lane_key():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="TRUCK_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="IDLE", error_message=None)
    idempotency = MagicMock()
    idempotency.start.return_value = SimpleNamespace(id="pm-cancel-no-lane-warning")
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CANCEL,
        data={},
        confidence=1.0,
        source="TEST",
        trace_id="trace-cancel-no-lane-warning",
    )

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_session_data", return_value={"current_city": "jaipur"}), \
             patch("app.routers.webhook.clear_session") as mock_clear_session, \
             patch("app.routers.webhook.logger.warning") as mock_warning:
            response, _, _ = await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.cancel.no-lane-warning",
                db=db,
                trace_id="trace-cancel-no-lane-warning",
                intent=Intent.CANCEL,
                payload={"action": "CANCEL"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )
            return response, mock_clear_session, mock_warning

    response, mock_clear_session, mock_warning = asyncio.run(run())

    assert "cancelled" in response.text.lower()
    mock_clear_session.assert_called_once_with(db, "919999999999")
    mock_warning.assert_not_called()


def test_terminal_workflow_clears_directional_lane_key():
    db = MagicMock()
    locked_user = SimpleNamespace(id="user-123", state="LOAD_FLOW", updated_at=None)
    transition = SimpleNamespace(allowed=True, next_state="IDLE", error_message=None)
    idempotency = MagicMock()
    idempotency.start.return_value = SimpleNamespace(id="pm-terminal-directional")
    state_machine = MagicMock()
    state_machine.transition.return_value = transition
    extraction = ExtractionResult(
        intent=Intent.CANCEL,
        data={},
        confidence=1.0,
        source="TEST",
        trace_id="trace-terminal-directional",
    )
    session_store = {
        "lane_key": "delhi:jaipur",
        "directional_lane_key": "delhi->jaipur",
        "reverse_directional_lane_key": "jaipur->delhi",
    }

    def fake_get_session_data(_db, _phone, _user_id, create=False):
        return dict(session_store)

    def fake_clear_session(_db, _phone):
        session_store.clear()

    async def run():
        with patch("app.routers.webhook._lock_user_for_dispatch", return_value=locked_user), \
             patch("app.routers.webhook.get_session_data", side_effect=fake_get_session_data), \
             patch("app.routers.webhook.clear_session", side_effect=fake_clear_session):
            return await _phase2_atomic_dispatch(
                phone="919999999999",
                wa_id="wamid.terminal-directional",
                db=db,
                trace_id="trace-terminal-directional",
                intent=Intent.CANCEL,
                payload={"action": "CANCEL"},
                extraction=extraction,
                idempotency=idempotency,
                state_machine=state_machine,
            )

    response, _, _ = asyncio.run(run())

    assert "cancelled" in response.text.lower()
    assert session_store == {}
