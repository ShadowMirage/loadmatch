import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.contracts.enums import Intent
from app.contracts.extraction import ExtractionResult
from app.contracts.payloads import CreateLoadPayload, PostTruckPayload
from app.routers.webhook import _phase1_resolve_intent
from app.services.dispatcher_service import DispatcherService
from app.services.intent_resolver import IntentResolver
from app.services.payload_factory import PayloadFactory


class _StubExtractionEngine:
    def __init__(self, result: ExtractionResult):
        self.result = result

    async def extract(self, text, user, session_data):
        return self.result


def test_weight_correction_before_confirm_uses_latest_value():
    async def run():
        msg = {"type": "text", "text": {"body": "actually 10 ton"}}
        user = SimpleNamespace(id="user-123", state="LOAD_FLOW")
        extraction_engine = _StubExtractionEngine(
            ExtractionResult(
                intent=Intent.UNKNOWN,
                data={"weight_kg": 10000},
                confidence=0.4,
                source="TEST",
                trace_id="trace-weight-correction",
            )
        )
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow="LOAD_FLOW")), \
             patch(
                 "app.routers.webhook.get_session_data",
                 return_value={
                     "from_city": "jaipur",
                     "to_city": "delhi",
                     "weight_kg": 7000,
                     "date": "tomorrow",
                     "lane_key": "delhi:jaipur",
                     "directional_lane_key": "jaipur->delhi",
                 },
             ):
            intent, payload, extraction, current_workflow = await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id="wamid.weight-correction",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )
            return intent, payload, extraction, current_workflow

    intent, payload, extraction, current_workflow = asyncio.run(run())

    assert current_workflow == "LOAD_FLOW"
    assert intent == Intent.CREATE_LOAD
    assert extraction.data["weight_kg"] == 10000
    assert isinstance(payload, CreateLoadPayload)
    assert payload.weight_kg == 10000
    assert payload.from_city == "jaipur"
    assert payload.to_city == "delhi"


def test_lane_key_not_recomputed_on_non_route_mutation():
    original_lane_key = "delhi:jaipur"
    original_directional_lane_key = "jaipur->delhi"

    async def run():
        msg = {"type": "text", "text": {"body": "7 ton"}}
        user = SimpleNamespace(id="user-123", state="LOAD_FLOW")
        extraction_engine = _StubExtractionEngine(
            ExtractionResult(
                intent=Intent.UNKNOWN,
                data={"weight_kg": 7000},
                confidence=0.4,
                source="TEST",
                trace_id="trace-lane-stability",
            )
        )
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow="LOAD_FLOW")), \
             patch(
                 "app.routers.webhook.get_session_data",
                 return_value={
                     "from_city": "jaipur",
                     "to_city": "delhi",
                     "lane_key": original_lane_key,
                     "directional_lane_key": original_directional_lane_key,
                     "confidence_source": "corridor_detection",
                     "corridor_source": "city_pair",
                 },
             ):
            return await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id="wamid.lane-stability",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )

    intent, payload, extraction, _ = asyncio.run(run())

    assert intent == Intent.CREATE_LOAD
    assert extraction.data["lane_key"] == original_lane_key
    assert extraction.data["directional_lane_key"] == original_directional_lane_key
    assert extraction.data["from_city"] == "jaipur"
    assert extraction.data["to_city"] == "delhi"
    assert payload.weight_kg == 7000


def test_confirm_stage_retains_corrected_fields():
    dispatcher = DispatcherService(MagicMock(), user_id="user-123", phone="919999999999")
    payload = SimpleNamespace(data={"weight_kg": 10000})

    with patch(
        "app.services.dispatcher_service.get_session_data",
        return_value={
            "from_city": "jaipur",
            "to_city": "delhi",
            "weight_kg": 7000,
            "date": "tomorrow",
        },
    ):
        response = dispatcher.execute(Intent.CREATE_LOAD, payload=payload, current_workflow="LOAD_CONFIRM")

    assert "Confirming your load details" in response.text
    assert "10000 kg" in response.text
    assert "7000 kg" not in response.text


def test_route_correction_after_confirm_stage_updates_lane_keys():
    async def run():
        msg = {"type": "text", "text": {"body": "delhi to mumbai"}}
        user = SimpleNamespace(id="user-123", state="LOAD_CONFIRM")
        extraction_engine = _StubExtractionEngine(
            ExtractionResult(
                intent=Intent.UNKNOWN,
                data={},
                confidence=0.0,
                source="TEST",
                trace_id="trace-route-correction",
            )
        )
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow="LOAD_CONFIRM")), \
             patch(
                 "app.routers.webhook.get_session_data",
                 return_value={
                     "from_city": "delhi",
                     "to_city": "jaipur",
                     "weight_kg": 7000,
                     "date": "tomorrow",
                     "lane_key": "delhi:jaipur",
                     "directional_lane_key": "delhi->jaipur",
                     "reverse_directional_lane_key": "jaipur->delhi",
                 },
             ):
            return await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id="wamid.route-correction",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )

    intent, payload, extraction, current_workflow = asyncio.run(run())

    assert current_workflow == "LOAD_CONFIRM"
    assert intent == Intent.CREATE_LOAD
    assert extraction.data["from_city"] == "delhi"
    assert extraction.data["to_city"] == "mumbai"
    assert extraction.data["lane_key"] == "delhi:mumbai"
    assert extraction.data["directional_lane_key"] == "delhi->mumbai"
    assert extraction.data["reverse_directional_lane_key"] == "mumbai->delhi"
    assert extraction.data["lane_key"] != "delhi:jaipur"
    assert extraction.data["directional_lane_key"] != "delhi->jaipur"
    assert payload.from_city == "delhi"
    assert payload.to_city == "mumbai"


def test_multi_field_correction_ordering_consistency():
    session_store = {
        "from_city": "delhi",
        "to_city": "jaipur",
        "weight_kg": 7000,
        "date": "tomorrow",
        "lane_key": "delhi:jaipur",
        "directional_lane_key": "delhi->jaipur",
        "reverse_directional_lane_key": "jaipur->delhi",
    }

    async def resolve_message(message_text: str, workflow: str, extraction_result: ExtractionResult):
        msg = {"type": "text", "text": {"body": message_text}}
        user = SimpleNamespace(id="user-123", state=workflow)
        extraction_engine = _StubExtractionEngine(extraction_result)
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow=workflow)), \
             patch("app.routers.webhook.get_session_data", return_value=dict(session_store)):
            intent, payload, extraction, current_workflow = await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id=f"wamid.{message_text}",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )
            session_store.update(extraction.data)
            return intent, payload, extraction, current_workflow

    route_result = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={},
        confidence=0.0,
        source="TEST",
        trace_id="trace-multi-route",
    )
    weight_result = ExtractionResult(
        intent=Intent.UNKNOWN,
        data={"weight_kg": 10000},
        confidence=0.4,
        source="TEST",
        trace_id="trace-multi-weight",
    )

    route_intent, route_payload, route_extraction, current_workflow = asyncio.run(
        resolve_message("delhi to mumbai", "LOAD_CONFIRM", route_result)
    )
    weight_intent, weight_payload, weight_extraction, current_workflow = asyncio.run(
        resolve_message("actually 10 ton", "LOAD_CONFIRM", weight_result)
    )

    assert current_workflow == "LOAD_CONFIRM"
    assert route_intent == Intent.CREATE_LOAD
    assert weight_intent == Intent.CREATE_LOAD
    assert route_extraction.data["to_city"] == "mumbai"
    assert route_extraction.data["lane_key"] == "delhi:mumbai"
    assert weight_extraction.data["weight_kg"] == 10000
    assert session_store["to_city"] == "mumbai"
    assert session_store["weight_kg"] == 10000
    assert session_store["lane_key"] == "delhi:mumbai"
    assert session_store["directional_lane_key"] == "delhi->mumbai"
    assert route_payload.to_city == "mumbai"
    assert weight_payload.weight_kg == 10000


def test_load_flow_capacity_alias_maps_to_weight_for_payload_build():
    async def run():
        msg = {"type": "text", "text": {"body": "alwar ghaziabad 6 ton tomorrow"}}
        user = SimpleNamespace(id="user-123", state="LOAD_FLOW")
        extraction_engine = _StubExtractionEngine(
            ExtractionResult(
                intent=Intent.UNKNOWN,
                data={"from_city": "alwar", "to_city": "ghaziabad", "capacity": "6 ton", "date": "tomorrow"},
                confidence=0.4,
                source="TEST",
                trace_id="trace-capacity-alias-load",
            )
        )
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow="LOAD_FLOW")), \
             patch("app.routers.webhook.get_session_data", return_value={"resolver_version": "v-test"}):
            return await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id="wamid.capacity-alias-load",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )

    intent, payload, extraction, current_workflow = asyncio.run(run())

    assert current_workflow == "LOAD_FLOW"
    assert intent == Intent.CREATE_LOAD
    assert extraction.data["capacity_kg"] == 6000
    assert extraction.data["weight_kg"] == 6000
    assert extraction.data["resolver_version"]
    assert isinstance(payload, CreateLoadPayload)
    assert payload.weight_kg == 6000


def test_unknown_route_defaults_to_truck_flow_when_active_workflow_is_truck():
    async def run():
        msg = {"type": "text", "text": {"body": "delhi jaipur"}}
        user = SimpleNamespace(id="user-123", state="TRUCK_FLOW")
        extraction_engine = _StubExtractionEngine(
            ExtractionResult(
                intent=Intent.UNKNOWN,
                data={"from_city": "delhi", "to_city": "jaipur", "weight_kg": 9000},
                confidence=0.4,
                source="TEST",
                trace_id="trace-unknown-route-truck-flow",
            )
        )
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow="TRUCK_FLOW")), \
             patch(
                 "app.routers.webhook.get_session_data",
                 return_value={
                     "lane_key": "delhi:jaipur",
                     "directional_lane_key": "delhi->jaipur",
                     "resolver_version": "v-test",
                 },
             ):
            return await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id="wamid.unknown-route-truck-flow",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )

    intent, payload, extraction, _ = asyncio.run(run())

    assert intent == Intent.POST_TRUCK
    assert extraction.data["capacity_kg"] == 9000
    assert isinstance(payload, PostTruckPayload)


def test_truck_flow_corrects_extracted_create_load_intent_to_post_truck():
    async def run():
        msg = {"type": "text", "text": {"body": "chennai to mumbai 10 ton tomorrow"}}
        user = SimpleNamespace(id="user-123", state="TRUCK_FLOW")
        extraction_engine = _StubExtractionEngine(
            ExtractionResult(
                intent=Intent.CREATE_LOAD,
                data={"from_city": "chennai", "to_city": "mumbai", "weight_kg": 10000, "date": "tomorrow"},
                confidence=1.0,
                source="TEST",
                trace_id="trace-truck-flow-intent-correction",
            )
        )
        resolver = IntentResolver()
        factory = PayloadFactory()
        idempotency = MagicMock()
        idempotency.fetch_cached_intent_data.return_value = None

        with patch("app.routers.webhook.peek_session", return_value=SimpleNamespace(current_workflow="TRUCK_FLOW")), \
             patch(
                 "app.routers.webhook.get_session_data",
                 return_value={"current_city": "chennai", "resolver_version": "v-test"},
             ):
            return await _phase1_resolve_intent(
                msg=msg,
                phone="919999999999",
                wa_id="wamid.truck-flow-intent-correction",
                user=user,
                db=MagicMock(),
                extraction_engine=extraction_engine,
                intent_resolver=resolver,
                payload_factory=factory,
                idempotency=idempotency,
            )

    intent, payload, extraction, _ = asyncio.run(run())

    assert intent == Intent.POST_TRUCK
    assert extraction.data["capacity_kg"] == 10000
    assert isinstance(payload, PostTruckPayload)
