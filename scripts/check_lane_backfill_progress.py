import logging

from app.database import SessionLocal
from app.services.debug_logger import DebugLogger

logger = logging.getLogger(__name__)


def check_progress(session) -> bool:
    status = DebugLogger.get_canonical_lane_key_backfill_status(session)
    total = status.get("total_rows", 0)
    remaining = status.get("remaining_rows", 0)
    populated = max(total - remaining, 0)
    print(f"{populated}/{total} populated")

    if status.get("canonical_lane_backfill_complete"):
        logger.info("LANE_KEY_BACKFILL_COMPLETE")
        return True
    return False


def main() -> int:
    with SessionLocal() as session:
        return 0 if check_progress(session) else 1


if __name__ == "__main__":
    raise SystemExit(main())
