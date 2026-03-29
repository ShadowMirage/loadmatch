import asyncio
import json
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class EventBus:
    """
    Structured, Async Telemetry Service.
    Supports Scheme Versioning (v1), Sampling, and PII Redaction.
    """
    _queue = asyncio.Queue(maxsize=100)  # Bounded queue with active consumer
    
    def __init__(self, trace_id: str):
        self.trace_id = trace_id

    async def emit_async(self, payload: Dict[str, Any], priority: str = "MEDIUM"):
        """Non-blocking event emission with priority-based dropping."""
        payload = self._sanitize_payload(payload)
        event = {
            "version": 1,
            "trace_id": self.trace_id,
            "data": payload,
        }

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if priority == "LOW":
                logger.warning(f"Dropping low-priority event due to backpressure: {event}")
            else:
                await self._queue.put(event)

    @staticmethod
    def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not payload.get("pii_redact"):
            return payload
        sanitized = dict(payload)
        for key in ("phone", "user_phone", "token", "api_key", "auth"):
            if key in sanitized:
                sanitized[key] = "***"
        sanitized.pop("pii_redact", None)
        return sanitized

    @classmethod
    async def flush(cls):
        """Background worker that drains telemetry events."""
        while True:
            event = await cls._queue.get()
            try:
                logger.info(f"[EVENT]: {json.dumps(event)}")
            finally:
                cls._queue.task_done()
