import asyncio
import json
import logging
import traceback
from typing import Dict, Any, List, Callable, Awaitable

logger = logging.getLogger(__name__)

# Type definition for event handlers: (event_data: dict) -> Awaitable[None]
HandlerFunc = Callable[[Dict[str, Any]], Awaitable[None]]

class EventBus:
    """
    Structured, Async Event Bus.
    Supports:
    1. Telemetry emission (via bounded queue)
    2. Subscriber handlers (for real-time notifications like MATCH_FOUND)
    """
    _queue = asyncio.Queue(maxsize=100)
    _handlers: Dict[str, List[HandlerFunc]] = {}
    
    def __init__(self, trace_id: str):
        self.trace_id = trace_id

    @classmethod
    def register_handler(cls, event_name: str, handler: HandlerFunc):
        """Registers a global handler for a specific event name."""
        if event_name not in cls._handlers:
            cls._handlers[event_name] = []
        cls._handlers[event_name].append(handler)
        logger.info(f"[EVENT_BUS] Registered handler for {event_name}")

    async def emit_async(self, payload: Dict[str, Any], priority: str = "MEDIUM"):
        """
        Emits an event to both the telemetry queue and all registered handlers.
        Handlers are executed in parallel with error isolation.
        """
        event_name = payload.get("event")
        if not event_name:
            logger.warning("EventBus.emit_async called without 'event' name in payload.")
            return

        # 1. Telemetry (Existing behavior)
        sanitized_telemetry = self._sanitize_payload(payload)
        telemetry_event = {
            "version": 1,
            "trace_id": self.trace_id,
            "data": sanitized_telemetry,
        }
        try:
            self._queue.put_nowait(telemetry_event)
        except asyncio.QueueFull:
            if priority != "LOW":
                await self._queue.put(telemetry_event)

        # 2. Handler Execution (New behavior)
        handlers = self._handlers.get(event_name, [])
        if handlers:
            # Execute handlers in parallel. return_exceptions=True ensures one 
            # handler crash doesn't stop others or the main request thread.
            tasks = [handler(payload) for handler in handlers]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    handler_name = handlers[i].__name__ if hasattr(handlers[i], "__name__") else "unknown"
                    logger.error(f"[EVENT_BUS] Handler '{handler_name}' failed for event '{event_name}': {result}")
                    logger.debug(traceback.format_exc())

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
                # In production, this might send to Mixpanel/Datadog
                logger.info(f"[TELEMETRY]: {json.dumps(event)}")
            except Exception as e:
                logger.error(f"Telemetry flush error: {e}")
            finally:
                cls._queue.task_done()
