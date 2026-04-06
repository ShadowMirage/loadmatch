from unittest.mock import MagicMock, patch

from app.contracts.enums import Intent
from app.services.dispatcher_service import DispatcherService
from app.services.payload_factory import PayloadFactory
from app.contracts.route_confidence import RouteConfidence


def _build_live_and_replay_payloads(intent: Intent, extraction_data: dict):
    factory = PayloadFactory()
    live_payload = factory.build(intent, extraction_data)
    request_payload = {
        "intent": intent.value,
        "payload": PayloadFactory.serialize(live_payload),
        "extraction_data": dict(extraction_data),
    }
    replay_payload = factory.build(intent, factory.extract_replay_data(request_payload))
    return live_payload, replay_payload


def test_replay_route_confidence_equivalence():
    extraction_data = {
        "from_city": "bangalore",
        "to_city": "delhi",
        "confidence_source": "corridor_detection",
        "corridor_source": "city_pair",
        "lane_key": "bangalore:delhi",
        "directional_lane_key": "bangalore->delhi",
    }
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")
    live_payload, replay_payload = _build_live_and_replay_payloads(Intent.CREATE_LOAD, extraction_data)

    live_confidence = dispatcher._route_confidence_class(dispatcher._collect_payload_data(live_payload))
    replay_confidence = dispatcher._route_confidence_class(dispatcher._collect_payload_data(replay_payload))

    assert live_confidence == RouteConfidence.HIGH
    assert replay_confidence == RouteConfidence.HIGH


def test_replay_prompt_order_identical():
    extraction_data = {
        "from_city": "jaipur",
        "to_city": "delhi",
        "confidence_source": "corridor_detection",
        "corridor_source": "adjacent_city_pair",
        "lane_key": "delhi:jaipur",
        "directional_lane_key": "jaipur->delhi",
    }
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")
    live_payload, replay_payload = _build_live_and_replay_payloads(Intent.CREATE_LOAD, extraction_data)

    live_response = dispatcher.execute(Intent.CREATE_LOAD, live_payload, current_workflow="IDLE")
    replay_response = dispatcher.execute(Intent.CREATE_LOAD, replay_payload, current_workflow="IDLE")

    assert live_response.text == replay_response.text
    assert "Just confirming the route: Jaipur → Delhi." in live_response.text
    assert "please share the load weight." in live_response.text.lower()


def test_replay_confirmation_text_identical():
    extraction_data = {
        "from_city": "jaipur",
        "to_city": "delhi",
        "weight_kg": 7000,
        "date": "tomorrow",
    }
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")
    live_payload, replay_payload = _build_live_and_replay_payloads(Intent.CREATE_LOAD, extraction_data)

    live_response = dispatcher.execute(Intent.CREATE_LOAD, live_payload, current_workflow="LOAD_FLOW")
    replay_response = dispatcher.execute(Intent.CREATE_LOAD, replay_payload, current_workflow="LOAD_FLOW")

    assert live_response.text == replay_response.text
    assert "Confirming your load details" in live_response.text
    assert "7000 kg" in live_response.text


def test_typed_payload_replay_preserves_corridor_metadata():
    extraction_data = {
        "from_city": "ankleshwar",
        "to_city": "vapi",
        "weight_kg": 7000,
        "confidence_source": "corridor_detection",
        "corridor_source": "industrial_zone_pair",
        "corridor_detected": True,
        "lane_key": "ankleshwar:vapi",
        "directional_lane_key": "ankleshwar->vapi",
        "resolver_version": "v-test",
    }
    dispatcher = DispatcherService(MagicMock(), user_id="user-123")
    live_payload, replay_payload = _build_live_and_replay_payloads(Intent.CREATE_LOAD, extraction_data)

    assert getattr(live_payload, "extraction_data")["corridor_detected"] is True
    assert getattr(replay_payload, "extraction_data")["corridor_source"] == "industrial_zone_pair"
    assert getattr(replay_payload, "extraction_data")["lane_key"] == "ankleshwar:vapi"

    live_confidence = dispatcher._route_confidence_class(dispatcher._collect_payload_data(live_payload))
    replay_confidence = dispatcher._route_confidence_class(dispatcher._collect_payload_data(replay_payload))

    assert live_confidence == RouteConfidence.HIGH
    assert replay_confidence == RouteConfidence.HIGH


def test_payload_factory_merge_precedence_contract():
    extraction_data = {
        "from_city": "bangalore",
        "to_city": "delhi",
        "weight_kg": 7000,
        "lane_key": "bangalore:delhi",
        "directional_lane_key": "bangalore->delhi",
        "confidence_source": "corridor_detection",
        "corridor_source": "city_pair",
        "resolver_version": "v-test",
    }
    dispatcher = DispatcherService(MagicMock(), user_id="user-123", phone="919999999999")
    payload = PayloadFactory().build(Intent.CREATE_LOAD, extraction_data)

    with patch(
        "app.services.dispatcher_service.get_session_data",
        return_value={
            "lane_key": "blr:delhi",
            "directional_lane_key": "blr->delhi",
            "from_city": "blr",
            "to_city": "delhi",
        },
    ):
        merged = dispatcher._collect_payload_data(payload)

    assert merged["lane_key"] == "bangalore:delhi"
    assert merged["directional_lane_key"] == "bangalore->delhi"
    assert merged["from_city"] == "bangalore"
