import dataclasses
import logging
import re
from typing import Any, Optional
from datetime import date, datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.payloads import CreateLoadPayload, PostTruckPayload
from app.contracts.responses import Response as ContractResponse, Button, Section, SectionRow
from app.contracts.enums import Intent
from app.contracts.meta_intents import INTERRUPT_INTENTS
from app.contracts.route_confidence import RouteConfidence
from app.config import settings
from app.models.load_request import LoadRequest
from app.models.listing import TruckSpaceListing
from app.models.match import Match
from app.models.rating import Rating
from app.models.truck import Truck
from app.models.user import User
from app.models.enums import LoadRequestStatus, ListingStatus, TruckType
from app.services.logistics_data import normalize_hub_name, RESOLVER_VERSION
from app.services.load_freshness_service import is_recent_duplicate_load
from app.services.marketplace_freshness_service import is_recent_duplicate_lane
from app.services.matching_service import (
    canonical_lane_key,
    find_matches_for_load_summary,
    find_matches_for_truck_summary,
)
from app.services.event_logger import track_event
from app.services.session_manager import get_session_data, set_session_data
from app.services.state_machine_service import StateMachineService
from app.services.date_parser import normalize_date

logger = logging.getLogger(__name__)

UNIQUE_LANE_OPEN_INDEX_NAME = "unique_user_lane_open"
UNIQUE_LANE_VEHICLE_OPEN_INDEX_NAME = "unique_user_lane_vehicle_open"


def clear_session(db: Session, phone: str) -> None:
    StateMachineService.clear_session_and_metadata(db, phone)


def _is_unique_lane_open_violation(error: IntegrityError) -> bool:
    original = getattr(error, "orig", None)
    diag = getattr(original, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name in {UNIQUE_LANE_OPEN_INDEX_NAME, UNIQUE_LANE_VEHICLE_OPEN_INDEX_NAME}:
        return True

    message = " ".join(
        part for part in (str(error), str(original) if original is not None else "") if part
    ).lower()
    if UNIQUE_LANE_OPEN_INDEX_NAME in message or UNIQUE_LANE_VEHICLE_OPEN_INDEX_NAME in message:
        return True

    return (
        "unique constraint failed" in message
        and "truck_space_listings.owner_id" in message
        and "truck_space_listings.canonical_lane_key" in message
        and (
            "truck_space_listings.vehicle_type" in message
            or UNIQUE_LANE_OPEN_INDEX_NAME in message
        )
    )

class DispatcherService:
    """
    'Dumb' executor. NO validation. NO branching.
    Executes Intent using Payload. Returns standardized Response.
    """
    def __init__(self, db: Session, user_id: str, phone: str = None):
        self.db = db
        self.user_id = user_id
        self.phone = phone

    def execute(self, intent: Intent, payload: Any, current_workflow: Optional[str] = None) -> ContractResponse:
        """
        'Dumb' executor. NO validation. NO branching.
        Executes Intent using Payload. Returns standardized ContractResponse.
        """
        # 1. Handle CONFIRMATION of flows (Actually hits DB)
        if intent == Intent.CONFIRM:
            if current_workflow == "LOAD_FLOW":
                return self._handle_confirm_load(payload)
            elif current_workflow == "TRUCK_FLOW":
                return self._handle_confirm_truck(payload)
            else:
                return ContractResponse(text="Nothing to confirm.")

        if intent == Intent.CANCEL:
            return self._main_menu_response("Okay, I cancelled that request.")

        # 2. Handle Collection / Initialization flows (Does NOT hit DB, just prompts)
        if intent == Intent.CREATE_LOAD:
            return self._handle_create_load_prompt(payload)
        if intent == Intent.POST_TRUCK:
            return self._handle_post_truck_prompt(payload)
        if intent == Intent.VIEW_LOADS:
            return self._handle_view_loads()
        if intent == Intent.VIEW_TRUCKS:
            return self._handle_view_trucks()
        if intent == Intent.UPLOAD_KYC:
            return self._handle_upload_kyc()
        if intent == Intent.RATE_TRIP:
            return self._handle_rate_trip(payload)
        if intent == Intent.TRACK_TRUCK:
            return self._handle_track_truck(payload)
        if intent == Intent.CONTACT_DRIVER:
            return self._handle_contact_driver(payload)
        if intent == Intent.CONFIRM_BOOKING:
            return self._handle_confirm_booking(payload)
        if intent == Intent.UNKNOWN and not StateMachineService.workflow_is_active(current_workflow):
            return self._main_menu_response("I didn't catch that. Please select an option:")

        if intent in INTERRUPT_INTENTS:
            return self._handle_menu_interrupt(current_workflow)

        return self._handle_menu_interrupt(current_workflow)

    def _track_internal_event(self, event_type: str, data: dict | None = None) -> None:
        try:
            track_event(self.db, self.user_id, event_type, data=data or {}, commit=False)
        except Exception:
            logger.debug("Failed to write internal event marker %s", event_type, exc_info=True)

    def _handle_create_load_prompt(self, payload: CreateLoadPayload) -> ContractResponse:
        """Guide the user through missing slots, then confirm collected data."""
        data = self._collect_payload_data(payload)
        
        # Validate Unknowns natively
        if data.get("from_city") and self._city_label(data.get("from_city")) == "Unknown":
            data.pop("from_city", None)
        if data.get("to_city") and self._city_label(data.get("to_city")) == "Unknown":
            data.pop("to_city", None)

        missing_fields = self._get_missing_load_fields(data)
        if missing_fields:
            if "route_confirmation" in missing_fields:
                self._mark_route_confirmation_prompted(data, flow="load")
                return self._ask_for_route_confirmation("load", data)
            return self._ask_for_missing_fields("load", missing_fields)

        from_city = self._city_label(data.get("from_city"))
        to_city = self._city_label(data.get("to_city"))
        weight = int(data.get("weight_kg") or 0)
        pickup_date = self._coerce_date(data.get("pickup_date") or data.get("date"))

        text = (
            f"Confirming your load details:\n"
            f"📍 From: {from_city}\n"
            f"🏁 To: {to_city}\n"
            f"⚖️ Weight: {weight} kg\n\n"
            f"📅 Pickup: {self._format_date(pickup_date)}\n\n"
            f"Is this correct?"
        )
        
        buttons = [
            Button(id="CONFIRM_LOAD", title="Confirm"),
            Button(id="CANCEL", title="Cancel"),
        ]
        
        return ContractResponse(text=text, buttons=buttons)

    def _handle_post_truck_prompt(self, payload: PostTruckPayload) -> ContractResponse:
        """Guide the user through missing slots, then confirm collected data."""
        data = self._collect_payload_data(payload)
        
        # Validate Unknowns natively
        if data.get("from_city") and self._city_label(data.get("from_city")) == "Unknown":
            data.pop("from_city", None)
        if data.get("to_city") and self._city_label(data.get("to_city")) == "Unknown":
            data.pop("to_city", None)
        if data.get("current_city") and self._city_label(data.get("current_city")) == "Unknown":
            data.pop("current_city", None)

        missing_fields = self._get_missing_truck_fields(data)
        if missing_fields:
            if "route_confirmation" in missing_fields:
                self._mark_route_confirmation_prompted(data, flow="truck")
                return self._ask_for_route_confirmation("truck", data)
            return self._ask_for_missing_fields("truck", missing_fields)

        from_city = self._city_label(data.get("current_city") or data.get("from_city"))
        to_city = self._city_label(data.get("to_city"))
        capacity = int(data.get("capacity_kg") or 0)
        departure_date = self._coerce_date(data.get("departure_date") or data.get("date"))

        text = (
            f"Confirming your truck availability:\n"
            f"📍 From: {from_city}\n"
            f"🏁 To: {to_city}\n"
            f"⚖️ Capacity: {capacity} kg\n\n"
            f"📅 Departure: {self._format_date(departure_date)}\n\n"
            f"Is this correct?"
        )
        
        buttons = [
            Button(id="CONFIRM_TRUCK", title="Confirm"),
            Button(id="CANCEL", title="Cancel"),
        ]
        
        return ContractResponse(text=text, buttons=buttons)

    def _handle_confirm_load(self, payload: Any) -> ContractResponse:
        """Actually create the LoadRequest in DB."""
        try:
            data = self._collect_payload_data(payload)
            if data.get("from_city") and self._city_label(data.get("from_city")) == "Unknown":
                data.pop("from_city", None)
            if data.get("to_city") and self._city_label(data.get("to_city")) == "Unknown":
                data.pop("to_city", None)

            missing_fields = self._get_missing_load_fields(data)
            if missing_fields:
                return self._ask_for_missing_fields("load", missing_fields)

            from_city = self._city_label(data.get("from_city"))
            to_city = self._city_label(data.get("to_city"))
            lane_key = canonical_lane_key(from_city, to_city)
            vehicle_type = self._coerce_vehicle_type(
                data.get("vehicle_type") or data.get("truck_type")
            )
            self._track_internal_event(
                "LOAD_CONFIRM_ATTEMPTED",
                {
                    "shipper_id": str(self.user_id),
                    "canonical_lane_key": lane_key,
                    "vehicle_type": vehicle_type,
                },
            )
            ranking_now = self._resolve_ranking_context_time(data)
            load = is_recent_duplicate_load(
                self.db,
                self.user_id,
                lane_key,
                vehicle_type,
                settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
            )
            if load is not None:
                logger.info(
                    "LOAD_FRESHNESS_WINDOW_BLOCKED",
                    extra={
                        "shipper_id": str(self.user_id),
                        "canonical_lane_key": lane_key,
                        "vehicle_type": vehicle_type,
                        "load_id": str(getattr(load, "id", "")),
                    },
                )
                self._track_internal_event(
                    "LOAD_FRESHNESS_WINDOW_BLOCKED",
                    {
                        "shipper_id": str(self.user_id),
                        "canonical_lane_key": lane_key,
                        "vehicle_type": vehicle_type,
                        "load_id": str(getattr(load, "id", "")),
                    },
                )
            else:
                load = LoadRequest(
                    shipper_id=self.user_id,
                    from_city=from_city,
                    to_city=to_city,
                    canonical_lane_key=lane_key,
                    vehicle_type=vehicle_type,
                    weight_kg=int(data.get("weight_kg") or 0),
                    budget_per_kg=self._coerce_float(data.get("budget_per_kg")),
                    goods_type=data.get("cargo") or data.get("material_type") or "General",
                    pickup_date=self._coerce_date(data.get("pickup_date") or data.get("date")),
                    status=LoadRequestStatus.open,
                )
                self.db.add(load)
                self.db.flush()

            matching_summary = self._safe_load_matching_summary(load, ranking_now=ranking_now)
            if matching_summary.get("match_count", 0) > 0:
                load.status = LoadRequestStatus.matched
            
            if self.phone:
                clear_session(self.db, self.phone)

            logger.info(f"Created LoadRequest {load.id} for user {self.user_id}")
            return ContractResponse(
                text=(
                    f"Load Created\n"
                    f"{load.from_city} → {load.to_city}\n"
                    f"{load.weight_kg} kg on {self._format_date(load.pickup_date)}"
                    f"{self._format_load_match_summary(matching_summary)}"
                )
            )
        except Exception as e:
            logger.error(f"Failed to confirm load: {e}")
            return ContractResponse(text="❌ Failed to post load. Please try again.")

    def _handle_confirm_truck(self, payload: Any) -> ContractResponse:
        """Actually create the Listing in DB."""
        try:
            data = self._collect_payload_data(payload)
            if data.get("from_city") and self._city_label(data.get("from_city")) == "Unknown":
                data.pop("from_city", None)
            if data.get("to_city") and self._city_label(data.get("to_city")) == "Unknown":
                data.pop("to_city", None)
            if data.get("current_city") and self._city_label(data.get("current_city")) == "Unknown":
                data.pop("current_city", None)

            missing_fields = self._get_missing_truck_fields(data)
            if missing_fields:
                return self._ask_for_missing_fields("truck", missing_fields)

            capacity_kg = int(data.get("capacity_kg") or 0)
            registration_number = (data.get("plate") or f"TRK{str(self.user_id).replace('-', '')[:8]}").upper()
            truck_type = self._coerce_truck_type(data.get("truck_type"))
            vehicle_type = str(getattr(truck_type, "value", truck_type) or "").strip().lower()
            from_city = self._city_label(data.get("current_city") or data.get("from_city"))
            to_city = self._city_label(data.get("to_city"))
            lane_key = canonical_lane_key(from_city, to_city)
            listing = is_recent_duplicate_lane(
                self.db,
                self.user_id,
                lane_key,
                vehicle_type,
                settings.MARKETPLACE_DUPLICATE_WINDOW_MINUTES,
            )
            if listing is not None:
                logger.info(
                    "FRESHNESS_WINDOW_BLOCKED",
                    extra={
                        "owner_id": str(self.user_id),
                        "canonical_lane_key": lane_key,
                        "vehicle_type": vehicle_type,
                    },
                )
                self._track_internal_event(
                    "LISTING_FRESHNESS_WINDOW_BLOCKED",
                    {
                        "owner_id": str(self.user_id),
                        "canonical_lane_key": lane_key,
                        "vehicle_type": vehicle_type,
                        "listing_id": str(getattr(listing, "id", "")),
                    },
                )

            else:
                truck = self.db.query(Truck).filter(Truck.owner_id == self.user_id).first()
                if not truck:
                    truck = Truck(
                        owner_id=self.user_id,
                        truck_type=truck_type,
                        total_capacity_kg=capacity_kg or 10000,
                        registration_number=registration_number,
                    )
                    self.db.add(truck)
                    self.db.flush()
                else:
                    truck.truck_type = truck_type
                    truck.total_capacity_kg = capacity_kg or truck.total_capacity_kg
                    truck.registration_number = registration_number

                vehicle_type = str(getattr(truck.truck_type, "value", truck.truck_type) or "").strip().lower()
                if not vehicle_type:
                    raise ValueError("vehicle_type must be set for listing persistence")

                listing = TruckSpaceListing(
                    owner_id=self.user_id,
                    truck_id=truck.id,
                    from_city=from_city,
                    to_city=to_city,
                    canonical_lane_key=lane_key,
                    vehicle_type=vehicle_type,
                    departure_date=self._coerce_date(data.get("departure_date") or data.get("date")),
                    total_capacity_kg=capacity_kg,
                    available_capacity_kg=capacity_kg,
                    price_per_kg=self._coerce_float(data.get("rate_per_kg"), default=0.0) or 0.0,
                    status=ListingStatus.open,
                )
                self.db.add(listing)
                try:
                    self.db.flush()
                except IntegrityError as exc:
                    if not _is_unique_lane_open_violation(exc):
                        raise

                    self.db.rollback()
                    logger.info(
                        "SCHEMA_DUPLICATE_LANE_SUPPRESSED",
                        extra={
                            "owner_id": str(self.user_id),
                            "canonical_lane_key": lane_key,
                            "vehicle_type": vehicle_type,
                            "index_name": UNIQUE_LANE_VEHICLE_OPEN_INDEX_NAME,
                        },
                    )
                    listing = self._find_existing_active_lane_listing(lane_key, vehicle_type)
                    if listing is None:
                        raise
                    self._track_internal_event(
                        "SCHEMA_DUPLICATE_LANE_SUPPRESSED",
                        {
                            "owner_id": str(self.user_id),
                            "canonical_lane_key": lane_key,
                            "vehicle_type": vehicle_type,
                            "listing_id": str(getattr(listing, "id", "")),
                            "index_name": UNIQUE_LANE_VEHICLE_OPEN_INDEX_NAME,
                        },
                    )

            self._track_internal_event(
                "LISTING_CONFIRM_ATTEMPTED",
                {
                    "owner_id": str(self.user_id),
                    "canonical_lane_key": lane_key,
                    "vehicle_type": vehicle_type,
                },
            )
            ranking_now = self._resolve_ranking_context_time(data)
            matching_summary = self._safe_truck_matching_summary(listing, ranking_now=ranking_now)

            if self.phone:
                clear_session(self.db, self.phone)

            logger.info(f"Created TruckSpaceListing {listing.id} for user {self.user_id}")
            return ContractResponse(
                text=(
                    f"Truck Posted\n"
                    f"{listing.from_city} → {listing.to_city}\n"
                    f"{listing.available_capacity_kg} kg on {self._format_date(listing.departure_date)}"
                    f"{self._format_truck_match_summary(matching_summary)}"
                )
            )
        except Exception as e:
            logger.error(f"Failed to confirm truck: {e}")
            return ContractResponse(text="❌ Failed to post truck. Please try again.")

    def _find_existing_active_lane_listing(self, lane_key: str, vehicle_type: str) -> Optional[TruckSpaceListing]:
        return (
            self.db.query(TruckSpaceListing)
            .filter(
                TruckSpaceListing.owner_id == self.user_id,
                TruckSpaceListing.canonical_lane_key == lane_key,
                TruckSpaceListing.vehicle_type == vehicle_type,
                TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial]),
            )
            .order_by(TruckSpaceListing.created_at.desc())
            .first()
        )

    def _handle_view_loads(self) -> ContractResponse:
        """List active loads for the user."""
        loads = self.db.query(LoadRequest)\
            .filter(LoadRequest.shipper_id == self.user_id)\
            .order_by(LoadRequest.created_at.desc())\
            .limit(5).all()

        if not loads:
            return ContractResponse(text="You have no active loads.")

        rows = [
            SectionRow(
                id=str(load.id),
                title=f"{load.from_city} → {load.to_city}",
                description=f"{load.weight_kg} kg | {self._format_date(load.pickup_date)}",
            )
            for load in loads
        ]
        return ContractResponse(
            text="Your recent loads",
            sections=[Section(title="Loads", rows=rows)],
            list_button_text="View Loads",
        )

    def _handle_view_trucks(self) -> ContractResponse:
        """List active truck listings for the user."""
        listings = self.db.query(TruckSpaceListing)\
            .filter(TruckSpaceListing.owner_id == self.user_id)\
            .order_by(TruckSpaceListing.created_at.desc())\
            .limit(5).all()

        if not listings:
            return ContractResponse(text="You have no active truck listings.")

        rows = [
            SectionRow(
                id=str(listing.id),
                title=f"{listing.from_city} → {listing.to_city}",
                description=f"{listing.available_capacity_kg} kg | {self._format_date(listing.departure_date)}",
            )
            for listing in listings
        ]
        return ContractResponse(
            text="Your recent truck listings",
            sections=[Section(title="Truck Listings", rows=rows)],
            list_button_text="View Trucks",
        )

    def _main_menu_response(self, prefix: str = "") -> ContractResponse:
        text_parts = [prefix] if prefix else []
        text_parts.append("Welcome to LoadMatch. How can I help you today?")
        buttons = [
            Button(id="POST_LOAD", title="Post Load"),
            Button(id="POST_TRUCK", title="Post Truck"),
            Button(id="UPLOAD_KYC", title="Upload KYC"),
        ]
        return ContractResponse(text="\n".join(text_parts), buttons=buttons)

    def _handle_upload_kyc(self) -> ContractResponse:
        return ContractResponse(
            text="KYC Upload\nPlease upload a photo of your RC or Driving License to complete KYC.",
            buttons=[Button(id="MAIN_MENU", title="Main Menu")],
        )

    def _handle_rate_trip(self, payload: Any) -> ContractResponse:
        action_id = self._extract_action_id(payload)
        match_id, score = self._match_and_score_from_action(action_id)
        if not match_id or score is None:
            return ContractResponse(text="Unable to record that rating.")

        match_obj = self.db.query(Match).filter(Match.id == match_id).first()
        if not match_obj:
            return ContractResponse(text="Unable to find that trip for rating.")

        load = self.db.query(LoadRequest).filter(LoadRequest.id == match_obj.load_request_id).first()
        listing = self.db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match_obj.listing_id).first()
        if not load or not listing:
            return ContractResponse(text="Unable to find that trip for rating.")

        if str(load.shipper_id) == str(self.user_id):
            rated_user_id = listing.owner_id
        elif str(listing.owner_id) == str(self.user_id):
            rated_user_id = load.shipper_id
        else:
            return ContractResponse(text="You are not part of that trip.")

        existing = self.db.query(Rating).filter(
            Rating.match_id == match_obj.id,
            Rating.rater_id == self.user_id,
        ).first()
        if existing:
            existing.score = float(score)
        else:
            self.db.add(
                Rating(
                    match_id=match_obj.id,
                    rater_id=self.user_id,
                    rated_user_id=rated_user_id,
                    score=float(score),
                )
            )

        if self.phone:
            clear_session(self.db, self.phone)

        return ContractResponse(text=f"Thanks. Your {score}/5 rating has been saved.")

    def _handle_track_truck(self, payload: Any) -> ContractResponse:
        booking_code = self._booking_code_from_action(payload, "TRACK_TRUCK_")
        match_obj = self._match_by_booking_code(booking_code)
        if not match_obj:
            return ContractResponse(text="Booking not found.")

        listing = self.db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match_obj.listing_id).first()
        if not listing:
            return ContractResponse(text="Truck details are unavailable for that booking.")

        eta_text = self._format_eta(match_obj.eta)
        location_text = self._format_driver_location(match_obj.driver_lat, match_obj.driver_lng)
        return ContractResponse(
            text=(
                f"Booking {booking_code}\n"
                f"Route: {listing.from_city} → {listing.to_city}\n"
                f"Status: {match_obj.status.value}\n"
                f"{eta_text}{location_text}"
            )
        )

    def _handle_contact_driver(self, payload: Any) -> ContractResponse:
        booking_code = self._booking_code_from_action(payload, "CONTACT_DRIVER_")
        match_obj = self._match_by_booking_code(booking_code)
        if not match_obj:
            return ContractResponse(text="Booking not found.")

        listing = self.db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match_obj.listing_id).first()
        if not listing:
            return ContractResponse(text="Driver contact is unavailable for that booking.")

        truck_owner_phone = None
        if getattr(listing, "owner", None) is not None:
            truck_owner_phone = getattr(listing.owner, "phone", None)
        if truck_owner_phone is None:
            owner = self.db.query(User).filter(User.id == listing.owner_id).first()
            truck_owner_phone = getattr(owner, "phone", None)

        if not truck_owner_phone:
            return ContractResponse(text="Driver contact is unavailable for that booking.")

        return ContractResponse(
            text=f"Booking {booking_code}\nDriver contact: {truck_owner_phone}"
        )

    def _handle_confirm_booking(self, payload: Any) -> ContractResponse:
        booking_code = self._booking_code_from_action(payload, "CONFIRM_BOOKING_")
        match_obj = self._match_by_booking_code(booking_code)
        if not match_obj:
            return ContractResponse(text="Booking not found.")

        return ContractResponse(
            text=(
                f"Booking {booking_code}\n"
                f"Current status: {match_obj.status.value}\n"
                f"Use Track Truck or Contact Driver for the next step."
            )
        )

    def _format_date(self, d: Optional[date]) -> str:
        if not d: return "Today"
        return d.strftime("%d-%m-%Y")

    def _coerce_date(self, val: Any) -> date:
        if isinstance(val, date): return val
        if not val: return date.today()
        try:
            normalized = normalize_date(str(val))
            if normalized:
                return datetime.strptime(normalized, "%d-%m-%Y").date()
        except Exception:
            pass
        return date.today()

    @staticmethod
    def _reference_code(prefix: str, entity_id: Any) -> str:
        return f"{prefix}{str(entity_id).replace('-', '').upper()[:8]}"

    @staticmethod
    def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        if value in (None, ""):
            return default
        return float(value)

    def _collect_payload_data(self, payload: Any) -> dict:
        payload_data = {}
        extraction_data = None
        if payload is None:
            payload_data = {}
        elif isinstance(payload, dict):
            payload_data = payload.get("data", payload)
            extraction_data = payload_data if isinstance(payload_data, dict) else None
        elif hasattr(payload, "data") and isinstance(payload.data, dict):
            payload_data = payload.data
            extraction_data = payload.data
        elif dataclasses.is_dataclass(payload):
            extraction_data = getattr(payload, "extraction_data", None)
            payload_data = {
                **(extraction_data if isinstance(extraction_data, dict) else {}),
                **dataclasses.asdict(payload),
            }
        else:
            payload_data = {
                key: value
                for key, value in getattr(payload, "__dict__", {}).items()
                if not key.startswith("_")
            }
            maybe_extraction = getattr(payload, "extraction_data", None)
            extraction_data = maybe_extraction if isinstance(maybe_extraction, dict) else None

        session_data = {}
        if self.phone:
            try:
                session_data = get_session_data(self.db, self.phone, str(self.user_id), create=False) or {}
            except Exception:
                session_data = {}

        if (
            isinstance(extraction_data, dict)
            and session_data
            and extraction_data.get("lane_key")
            and session_data.get("lane_key")
            and extraction_data.get("lane_key") != session_data.get("lane_key")
        ):
            logger.warning(
                "AUTHORITY_DRIFT_DETECTED",
                extra={
                    "session_lane_key": session_data.get("lane_key"),
                    "extraction_lane_key": extraction_data.get("lane_key"),
                    "directional_lane_key": extraction_data.get("directional_lane_key"),
                },
            )

        if isinstance(extraction_data, dict):
            if extraction_data.get("from_city") and extraction_data.get("to_city"):
                session_data["from_city"] = extraction_data.get("from_city")
                session_data["to_city"] = extraction_data.get("to_city")

        clean_payload = {k: v for k, v in (payload_data or {}).items() if v is not None}

        merged = {}
        merged.update(session_data or {})
        merged.update(extraction_data or {})
        merged.update(clean_payload or {})
        if not merged.get("resolver_version"):
            merged["resolver_version"] = (
                clean_payload.get("resolver_version")
                or (extraction_data or {}).get("resolver_version")
                or (session_data or {}).get("resolver_version")
                or RESOLVER_VERSION
            )
        return merged

    @staticmethod
    def _city_label(value: Any) -> str:
        normalized = normalize_hub_name(str(value or ""))
        return normalized.title() if normalized else "Unknown"

    @staticmethod
    def _normalized_metadata_value(value: Any) -> str:
        raw_value = getattr(value, "value", value)
        return str(raw_value or "").strip().lower()

    def _route_confidence_class(self, data: dict) -> RouteConfidence:
        if not data.get("resolver_version"):
            logger.warning(
                "[MISSING_RESOLVER_VERSION]",
                extra={
                    "lane_key": data.get("lane_key"),
                    "directional_lane_key": data.get("directional_lane_key"),
                },
            )
        confidence_source = self._normalized_metadata_value(data.get("confidence_source"))
        corridor_source = self._normalized_metadata_value(data.get("corridor_source"))

        if not confidence_source:
            logger.warning(
                "Dispatcher pacing fallback: extraction_data missing confidence_source",
                extra={
                    "lane_key": data.get("lane_key"),
                    "directional_lane_key": data.get("directional_lane_key"),
                },
            )
            return RouteConfidence.LOW

        if confidence_source == "corridor_detection":
            if corridor_source in {"city_pair", "alias_pair", "industrial_zone_pair"}:
                return RouteConfidence.HIGH
            if corridor_source == "adjacent_city_pair":
                return RouteConfidence.MEDIUM
        if confidence_source == "llm_structured":
            return RouteConfidence.MEDIUM
        return RouteConfidence.LOW

    @staticmethod
    def _load_route_present(data: dict) -> bool:
        return bool(data.get("from_city")) and bool(data.get("to_city"))

    @staticmethod
    def _truck_route_present(data: dict) -> bool:
        return bool(data.get("current_city") or data.get("from_city")) and bool(data.get("to_city"))

    @staticmethod
    def _next_missing_field(data: dict, ordered_fields: tuple[str, ...], aliases: Optional[dict[str, tuple[str, ...]]] = None) -> Optional[str]:
        aliases = aliases or {}
        for field in ordered_fields:
            candidates = aliases.get(field, (field,))
            if not any(data.get(candidate) not in (None, "") for candidate in candidates):
                return field
        return None

    def _route_signature(self, data: dict, flow: str) -> Optional[str]:
        origin = data.get("from_city")
        if flow == "truck":
            origin = data.get("current_city") or origin
        destination = data.get("to_city")
        if not origin or not destination:
            return None
        return str(data.get("directional_lane_key") or f"{origin}->{destination}")

    def _route_confirmation_prompted(self, data: dict, flow: str) -> bool:
        signature = self._route_signature(data, flow)
        if not signature:
            return False
        return str(data.get("route_confirmation_prompted_for") or "") == signature

    def _mark_route_confirmation_prompted(self, data: dict, flow: str) -> None:
        signature = self._route_signature(data, flow)
        if not signature or not self.phone:
            return
        set_session_data(
            self.db,
            self.phone,
            str(self.user_id),
            {"route_confirmation_prompted_for": signature},
        )

    def _get_missing_load_fields(self, data: dict) -> list[str]:
        route_confidence = self._route_confidence_class(data)
        missing = []
        if self._next_missing_field(data, ("from_city",)): missing.append("from_city")
        if self._next_missing_field(data, ("to_city",)): missing.append("to_city")
        if self._next_missing_field(data, ("weight_kg",)): missing.append("weight_kg")
        if self._next_missing_field(data, ("date",), aliases={"date": ("pickup_date", "date")}): missing.append("date")

        route_missing = "from_city" in missing or "to_city" in missing
        non_route_missing = "weight_kg" in missing or "date" in missing

        if (
            route_confidence == RouteConfidence.MEDIUM
            and not route_missing
            and non_route_missing
            and not self._route_confirmation_prompted(data, flow="load")
        ):
            return ["route_confirmation"] + missing

        return missing

    def _get_missing_truck_fields(self, data: dict) -> list[str]:
        route_confidence = self._route_confidence_class(data)
        missing = []
        if self._next_missing_field(data, ("from_city",), aliases={"from_city": ("current_city", "from_city")}): missing.append("from_city")
        if self._next_missing_field(data, ("to_city",)): missing.append("to_city")
        if self._next_missing_field(data, ("capacity_kg",)): missing.append("capacity_kg")
        if self._next_missing_field(data, ("date",), aliases={"date": ("departure_date", "date")}): missing.append("date")

        route_missing = "from_city" in missing or "to_city" in missing
        non_route_missing = "capacity_kg" in missing or "date" in missing

        if (
            route_confidence == RouteConfidence.MEDIUM
            and not route_missing
            and non_route_missing
            and not self._route_confirmation_prompted(data, flow="truck")
        ):
            return ["route_confirmation"] + missing

        return missing

    @staticmethod
    def _ask_for_missing_fields(flow: str, fields: list[str]) -> ContractResponse:
        slot_labels = {
            "from_city": "Pickup city",
            "to_city": "Destination city",
            "capacity_kg": "Truck capacity" if flow == "truck" else "Capacity",
            "weight_kg": "Load weight",
            "date": "Departure date" if flow == "truck" else "Pickup date",
        }
        
        display_fields = [f for f in fields if f in slot_labels]
        
        if not display_fields:
            return ContractResponse(text="Please share the missing details.")

        formatted = "\n".join(
            f"{i+1}. {slot_labels[s]}"
            for i, s in enumerate(display_fields)
        )
        return ContractResponse(text=f"Please provide:\n{formatted}")

    def _ask_for_route_confirmation(self, flow: str, data: dict) -> ContractResponse:
        if flow == "truck":
            origin = self._city_label(data.get("current_city") or data.get("from_city"))
            missing_fields = self._get_missing_truck_fields(data)
        else:
            origin = self._city_label(data.get("from_city"))
            missing_fields = self._get_missing_load_fields(data)

        destination = self._city_label(data.get("to_city"))
        display_fields = [f for f in missing_fields if f != "route_confirmation"]

        if display_fields:
            follow_up = self._ask_for_missing_fields(flow, display_fields).text
            follow_up = follow_up[0].lower() + follow_up[1:]
        else:
            follow_up = "reply with any correction if this route is wrong."

        return ContractResponse(
            text=f"Just confirming the route: {origin} → {destination}.\nIf that's right, {follow_up}"
        )

    @staticmethod
    def _coerce_truck_type(value: Any) -> TruckType:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        for truck_type in TruckType:
            if truck_type.value == normalized:
                return truck_type
        return TruckType.medium

    @staticmethod
    def _coerce_vehicle_type(value: Any) -> str | None:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        if not normalized:
            return None
        for truck_type in TruckType:
            if truck_type.value == normalized:
                return truck_type.value
        return None

    @staticmethod
    def _resolve_ranking_context_time(data: dict) -> datetime:
        raw_value = data.get("ranking_context_timestamp")
        if not raw_value:
            return datetime.now(timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(raw_value))
        except Exception:
            logger.warning(
                "INVALID_RANKING_CONTEXT_TIMESTAMP",
                extra={"raw_value": raw_value},
            )
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _extract_action_id(self, payload: Any) -> str:
        data = self._collect_payload_data(payload)
        return str(data.get("interactive_action_id") or "").strip()

    def _booking_code_from_action(self, payload: Any, prefix: str) -> Optional[str]:
        action_id = self._extract_action_id(payload)
        if not action_id.startswith(prefix):
            return None
        return action_id[len(prefix):] or None

    def _match_by_booking_code(self, booking_code: Optional[str]) -> Optional[Match]:
        if not booking_code:
            return None
        return self.db.query(Match).filter(Match.booking_code == booking_code).first()

    @staticmethod
    def _match_and_score_from_action(action_id: str) -> tuple[Optional[str], Optional[int]]:
        rating_match = re.fullmatch(r"RATING_(.+)_(\d)", action_id or "")
        if not rating_match:
            return None, None
        return rating_match.group(1), int(rating_match.group(2))

    @staticmethod
    def _format_eta(eta: Optional[date]) -> str:
        if not eta:
            return "ETA: pending\n"
        return f"ETA: {eta}\n"

    @staticmethod
    def _format_driver_location(lat: Optional[float], lng: Optional[float]) -> str:
        if lat is None or lng is None:
            return "Live location: unavailable"
        return f"Live location: {lat}, {lng}"

    def _safe_load_matching_summary(
        self,
        load: LoadRequest,
        *,
        ranking_now: datetime | None = None,
    ) -> dict:
        try:
            return find_matches_for_load_summary(self.db, load, commit=False, now=ranking_now)
        except Exception as exc:
            logger.warning(f"Load matching skipped for {load.id}: {exc}")
            return {"match_count": 0, "matches": []}

    def _safe_truck_matching_summary(
        self,
        listing: TruckSpaceListing,
        *,
        ranking_now: datetime | None = None,
    ) -> dict:
        try:
            return find_matches_for_truck_summary(self.db, listing, now=ranking_now)
        except Exception as exc:
            logger.warning(f"Truck matching skipped for {listing.id}: {exc}")
            return {"match_count": 0, "matches": []}

    @staticmethod
    def _format_load_match_summary(summary: dict) -> str:
        if summary.get("match_count", 0) <= 0:
            fallback_routes = summary.get("fallback_suggestions") or []
            if fallback_routes:
                suggestions = ", ".join(f"{origin} → {destination}" for origin, destination in fallback_routes[:3])
                return f"\n\nNo exact truck matches yet. Nearby lanes: {suggestions}"
            return "\n\nNo truck matches yet."

        lines = [f"\n\nFound {summary['match_count']} matching truck(s):"]
        for index, match in enumerate(summary.get("matches", [])[:3], start=1):
            lines.append(
                f"{index}. {match['pickup']} → {match['drop']} | "
                f"{match['weight']} kg | {match['match_score']}% match"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_truck_match_summary(summary: dict) -> str:
        if summary.get("match_count", 0) <= 0:
            return "\n\nNo matching loads yet."

        lines = [f"\n\nFound {summary['match_count']} matching load(s):"]
        for index, match in enumerate(summary.get("matches", [])[:3], start=1):
            lines.append(
                f"{index}. {match['pickup']} → {match['drop']} | "
                f"{match['weight']} kg | {match['match_score']}% match"
            )
        return "\n".join(lines)

    def _is_confirmation_step(self, workflow: Optional[str]) -> bool:
        return StateMachineService.workflow_is_confirm_stage(workflow)

    def _handle_menu_interrupt(self, workflow: Optional[str]) -> ContractResponse:
        if StateMachineService.workflow_is_active(workflow) and not self._is_confirmation_step(workflow):
            return ContractResponse(
                text=(
                    "You're currently in a workflow.\n\n"
                    "1️⃣ Continue\n"
                    "2️⃣ Cancel\n"
                    "3️⃣ Main Menu"
                )
            )
        
        if self._is_confirmation_step(workflow):
            return ContractResponse(
                text="You are confirming an action.\n\nReply:\n1️⃣ Continue\n2️⃣ Cancel"
            )

        return self._main_menu_response()
