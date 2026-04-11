import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, func, inspect, or_
from sqlalchemy.orm import Session

from app.models.enums import ListingStatus, LoadRequestStatus
from app.models.event import EventLog
from app.models.listing import TruckSpaceListing
from app.models.load_request import LoadRequest
from app.models.processed_message import ProcessedMessage
from app.services.event_logger import track_event

logger = logging.getLogger(__name__)

ACTIVE_LISTING_STATUSES = (ListingStatus.open, ListingStatus.partial)
ACTIVE_LOAD_STATUSES = (LoadRequestStatus.open, LoadRequestStatus.matched)

LOAD_ATTEMPT_EVENT = "LOAD_CONFIRM_ATTEMPTED"
LOAD_BLOCKED_EVENT = "LOAD_FRESHNESS_WINDOW_BLOCKED"
LISTING_ATTEMPT_EVENT = "LISTING_CONFIRM_ATTEMPTED"
LISTING_BLOCKED_EVENT = "LISTING_FRESHNESS_WINDOW_BLOCKED"
LISTING_SCHEMA_BLOCKED_EVENT = "SCHEMA_DUPLICATE_LANE_SUPPRESSED"


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


class InternalMonitoringService:
    def __init__(
        self,
        session_factory,
        *,
        check_interval_seconds: int = 600,
        stale_executing_minutes: int = 5,
    ):
        self.session_factory = session_factory
        self.check_interval_seconds = check_interval_seconds
        self.stale_executing_minutes = stale_executing_minutes
        self._last_drift_signature: tuple[int, ...] | None = None

    async def run_forever(self) -> None:
        logger.info(
            "InternalMonitoringService started (interval=%ss stale_executing=%sm)",
            self.check_interval_seconds,
            self.stale_executing_minutes,
        )
        while True:
            await self.scan_constraint_drift_once()
            await asyncio.sleep(self.check_interval_seconds)

    async def scan_constraint_drift_once(self) -> None:
        db = self.session_factory()
        try:
            metrics = self.get_constraint_drift_metrics(
                db,
                stale_executing_minutes=self.stale_executing_minutes,
            )
            if metrics.get("schema_support_status") == "partial":
                self._last_drift_signature = None
                logger.info(
                    "CONSTRAINT_DRIFT_SCHEMA_PARTIAL_SUPPORT",
                    extra={"schema_support": metrics.get("schema_support")},
                )
                return
            if metrics["total_violations"] <= 0:
                self._last_drift_signature = None
                return

            signature = (
                metrics["listing_canonical_lane_key_null_count"],
                metrics["listing_vehicle_type_null_count"],
                metrics["load_canonical_lane_key_null_count"],
                metrics["load_vehicle_type_null_count"],
                metrics["duplicate_active_listing_groups"],
                metrics["duplicate_active_load_groups"],
                metrics["stale_executing_count"],
                metrics["request_payload_null_count"],
            )
            if signature == self._last_drift_signature:
                return

            logger.error("CONSTRAINT_DRIFT_DETECTED", extra=metrics)
            track_event(
                db,
                None,
                "CONSTRAINT_DRIFT_DETECTED",
                data=metrics,
                commit=False,
            )
            db.commit()
            self._last_drift_signature = signature
        except Exception:
            db.rollback()
            logger.exception("Constraint drift scan failed")
        finally:
            db.close()

    @staticmethod
    def _table_column_names(db: Session, table_name: str) -> set[str]:
        try:
            inspector = inspect(db.get_bind())
            return {
                str(column.get("name"))
                for column in inspector.get_columns(table_name)
                if column.get("name")
            }
        except Exception:
            logger.warning(
                "CONSTRAINT_DRIFT_SCHEMA_INTROSPECTION_FAILED",
                extra={"table": table_name},
                exc_info=True,
            )
            return set()

    @staticmethod
    def get_constraint_drift_metrics(
        db: Session,
        *,
        now: datetime | None = None,
        stale_executing_minutes: int = 5,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(minutes=stale_executing_minutes)

        listing_columns = InternalMonitoringService._table_column_names(db, "truck_space_listings")
        load_columns = InternalMonitoringService._table_column_names(db, "load_requests")

        listing_has_canonical_lane_key = "canonical_lane_key" in listing_columns
        listing_has_vehicle_type = "vehicle_type" in listing_columns
        listing_has_directional_lane_key = "directional_lane_key" in listing_columns
        listing_has_reverse_directional_lane_key = "reverse_directional_lane_key" in listing_columns
        load_has_canonical_lane_key = "canonical_lane_key" in load_columns
        load_has_vehicle_type = "vehicle_type" in load_columns
        load_has_directional_lane_key = "directional_lane_key" in load_columns
        load_has_reverse_directional_lane_key = "reverse_directional_lane_key" in load_columns

        listing_required = {
            "canonical_lane_key",
            "vehicle_type",
            "directional_lane_key",
            "reverse_directional_lane_key",
        }
        load_required = {
            "canonical_lane_key",
            "vehicle_type",
            "directional_lane_key",
            "reverse_directional_lane_key",
        }
        schema_support_status = "full"
        if not listing_required.issubset(listing_columns) or not load_required.issubset(load_columns):
            schema_support_status = "partial"

        listing_canonical_lane_key_null_count = (
            db.query(TruckSpaceListing)
            .filter(TruckSpaceListing.canonical_lane_key.is_(None))
            .count()
            if listing_has_canonical_lane_key
            else 0
        )
        listing_vehicle_type_null_count = (
            db.query(TruckSpaceListing)
            .filter(TruckSpaceListing.vehicle_type.is_(None))
            .count()
            if listing_has_vehicle_type
            else 0
        )
        load_canonical_lane_key_null_count = (
            db.query(LoadRequest)
            .filter(LoadRequest.canonical_lane_key.is_(None))
            .count()
            if load_has_canonical_lane_key
            else 0
        )
        load_vehicle_type_null_count = (
            db.query(LoadRequest)
            .filter(LoadRequest.vehicle_type.is_(None))
            .count()
            if load_has_vehicle_type
            else 0
        )

        duplicate_active_listing_groups = 0
        if listing_has_canonical_lane_key and listing_has_vehicle_type:
            duplicate_active_listing_groups = (
                db.query(func.count())
                .select_from(
                    db.query(
                        TruckSpaceListing.owner_id,
                        TruckSpaceListing.canonical_lane_key,
                        TruckSpaceListing.vehicle_type,
                    )
                    .filter(TruckSpaceListing.status.in_(ACTIVE_LISTING_STATUSES))
                    .group_by(
                        TruckSpaceListing.owner_id,
                        TruckSpaceListing.canonical_lane_key,
                        TruckSpaceListing.vehicle_type,
                    )
                    .having(func.count() > 1)
                    .subquery()
                )
                .scalar()
                or 0
            )

        duplicate_active_load_groups = 0
        if load_has_canonical_lane_key and load_has_vehicle_type:
            duplicate_active_load_groups = (
                db.query(func.count())
                .select_from(
                    db.query(
                        LoadRequest.shipper_id,
                        LoadRequest.canonical_lane_key,
                        LoadRequest.vehicle_type,
                    )
                    .filter(LoadRequest.status.in_(ACTIVE_LOAD_STATUSES))
                    .group_by(
                        LoadRequest.shipper_id,
                        LoadRequest.canonical_lane_key,
                        LoadRequest.vehicle_type,
                    )
                    .having(func.count() > 1)
                    .subquery()
                )
                .scalar()
                or 0
            )

        stale_executing_count = (
            db.query(ProcessedMessage)
            .filter(
                ProcessedMessage.delivery_state == "EXECUTING",
                ProcessedMessage.updated_at < stale_cutoff,
            )
            .count()
        )
        request_payload_null_count = (
            db.query(ProcessedMessage)
            .filter(
                or_(
                    ProcessedMessage.request_payload.is_(None),
                    cast(ProcessedMessage.request_payload, String) == "null",
                )
            )
            .count()
        )

        total_violations = (
            listing_canonical_lane_key_null_count
            + listing_vehicle_type_null_count
            + load_canonical_lane_key_null_count
            + load_vehicle_type_null_count
            + duplicate_active_listing_groups
            + duplicate_active_load_groups
            + stale_executing_count
            + request_payload_null_count
        )

        return {
            "checked_at": now.isoformat(),
            "listing_canonical_lane_key_null_count": listing_canonical_lane_key_null_count,
            "listing_vehicle_type_null_count": listing_vehicle_type_null_count,
            "load_canonical_lane_key_null_count": load_canonical_lane_key_null_count,
            "load_vehicle_type_null_count": load_vehicle_type_null_count,
            "duplicate_active_listing_groups": duplicate_active_listing_groups,
            "duplicate_active_load_groups": duplicate_active_load_groups,
            "stale_executing_count": stale_executing_count,
            "request_payload_null_count": request_payload_null_count,
            "total_violations": total_violations,
            "schema_support_status": schema_support_status,
            "schema_support": {
                "truck_space_listings": {
                    "canonical_lane_key": listing_has_canonical_lane_key,
                    "vehicle_type": listing_has_vehicle_type,
                    "directional_lane_key": listing_has_directional_lane_key,
                    "reverse_directional_lane_key": listing_has_reverse_directional_lane_key,
                },
                "load_requests": {
                    "canonical_lane_key": load_has_canonical_lane_key,
                    "vehicle_type": load_has_vehicle_type,
                    "directional_lane_key": load_has_directional_lane_key,
                    "reverse_directional_lane_key": load_has_reverse_directional_lane_key,
                },
            },
        }

    @staticmethod
    def get_liquidity_health_snapshot(
        db: Session,
        *,
        now: datetime | None = None,
        lookback_hours: int = 24,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        event_cutoff = now - timedelta(hours=lookback_hours)

        active_listing_count = (
            db.query(TruckSpaceListing)
            .filter(TruckSpaceListing.status.in_(ACTIVE_LISTING_STATUSES))
            .count()
        )
        active_load_count = (
            db.query(LoadRequest)
            .filter(LoadRequest.status.in_(ACTIVE_LOAD_STATUSES))
            .count()
        )

        listing_lanes = {
            str(row[0])
            for row in db.query(TruckSpaceListing.canonical_lane_key)
            .filter(
                TruckSpaceListing.status.in_(ACTIVE_LISTING_STATUSES),
                TruckSpaceListing.canonical_lane_key.isnot(None),
            )
            .distinct()
            .all()
        }
        load_lanes = {
            str(row[0])
            for row in db.query(LoadRequest.canonical_lane_key)
            .filter(
                LoadRequest.status.in_(ACTIVE_LOAD_STATUSES),
                LoadRequest.canonical_lane_key.isnot(None),
            )
            .distinct()
            .all()
        }
        active_lanes = listing_lanes.union(load_lanes)

        listing_segments = {
            str(row[0])
            for row in db.query(TruckSpaceListing.vehicle_type)
            .filter(
                TruckSpaceListing.status.in_(ACTIVE_LISTING_STATUSES),
                TruckSpaceListing.vehicle_type.isnot(None),
            )
            .distinct()
            .all()
        }
        load_segments = {
            str(row[0])
            for row in db.query(LoadRequest.vehicle_type)
            .filter(
                LoadRequest.status.in_(ACTIVE_LOAD_STATUSES),
                LoadRequest.vehicle_type.isnot(None),
            )
            .distinct()
            .all()
        }
        vehicle_segments = listing_segments.union(load_segments)

        listing_lane_count = max(len(listing_lanes), 1)
        load_lane_count = max(len(load_lanes), 1)

        grouped_counts = (
            db.query(EventLog.event_type, func.count())
            .filter(
                EventLog.created_at >= event_cutoff,
                EventLog.event_type.in_(
                    [
                        LOAD_ATTEMPT_EVENT,
                        LOAD_BLOCKED_EVENT,
                        LISTING_ATTEMPT_EVENT,
                        LISTING_BLOCKED_EVENT,
                        LISTING_SCHEMA_BLOCKED_EVENT,
                    ]
                ),
            )
            .group_by(EventLog.event_type)
            .all()
        )
        event_counts = {str(event_type): int(count) for event_type, count in grouped_counts}

        load_attempts = event_counts.get(LOAD_ATTEMPT_EVENT, 0)
        load_blocked = event_counts.get(LOAD_BLOCKED_EVENT, 0)
        listing_attempts = event_counts.get(LISTING_ATTEMPT_EVENT, 0)
        listing_blocked = event_counts.get(LISTING_BLOCKED_EVENT, 0) + event_counts.get(
            LISTING_SCHEMA_BLOCKED_EVENT, 0
        )

        total_attempts = load_attempts + listing_attempts
        total_blocked = load_blocked + listing_blocked

        return {
            "generated_at": now.isoformat(),
            "active_lanes": len(active_lanes),
            "vehicle_segments": len(vehicle_segments),
            "active_listing_count": active_listing_count,
            "active_load_count": active_load_count,
            "avg_lane_supply": round(active_listing_count / listing_lane_count, 4),
            "avg_lane_demand": round(active_load_count / load_lane_count, 4),
            "imbalance_ratio": _safe_rate(active_load_count, max(active_listing_count, 1)),
            "freshness_suppression_rate": _safe_rate(total_blocked, total_attempts),
            "duplicate_load_reuse_rate": _safe_rate(load_blocked, load_attempts),
            "duplicate_listing_reuse_rate": _safe_rate(listing_blocked, listing_attempts),
            "event_window_hours": lookback_hours,
            "event_counts": {
                "load_attempts": load_attempts,
                "load_blocked": load_blocked,
                "listing_attempts": listing_attempts,
                "listing_blocked": listing_blocked,
            },
        }
