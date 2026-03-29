import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Hardcoded Architectural Parameters (Phase 10.1)
ZOMBIE_THRESHOLD_SECONDS = 300
REPLAY_THROTTLE_SECONDS = 30

# Stable Instance ID for the current worker process
_INSTANCE_ID = uuid.uuid4()

def get_instance_id() -> uuid.UUID:
    """Returns the unique identifier for this worker instance."""
    return _INSTANCE_ID

def is_zombie(record: Any) -> bool:
    """
    Unified Zombie Contract.
    A record is a zombie if it is IN_PROGRESS and hasn't been updated 
    within the ZOMBIE_THRESHOLD.
    """
    if record.status != "IN_PROGRESS":
        return False
    
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS)
    # Ensure update_at is timezone-aware for comparison if it's not already
    updated_at = record.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
        
    return updated_at < cutoff

def is_throttled(record: Any) -> bool:
    """
    Replay Throttling Guard.
    Prevents tight replay loops during database or external service instability.
    """
    if not record.recovery_attempted_at:
        return False
        
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=REPLAY_THROTTLE_SECONDS)
    attempted_at = record.recovery_attempted_at
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
        
    return attempted_at > cutoff
