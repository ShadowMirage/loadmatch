import asyncio
import logging
import random
from enum import Enum
from typing import Optional
from sqlalchemy.orm import Session
from app.contracts.responses import Response as ContractResponse

logger = logging.getLogger(__name__)


class DeliveryResult(str, Enum):
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED_SANDBOX = "skipped_sandbox"


def _extract_whatsapp_error_code(error: Exception) -> Optional[int]:
    response = getattr(error, "response", None)
    if response is not None:
        try:
            payload = response.json()
            error_payload = payload.get("error") if isinstance(payload, dict) else None
            code = error_payload.get("code") if isinstance(error_payload, dict) else None
            if code is not None:
                return int(code)
        except Exception:
            pass

        response_text = str(getattr(response, "text", "") or "")
        if "#131030" in response_text or "131030" in response_text:
            return 131030

    error_text = str(error)
    if "#131030" in error_text or "131030" in error_text:
        return 131030
    return None

class RecoveryService:
    def __init__(self, db: Session):
        self.db = db

    def recover_zombie_jobs(self):
        """
        DEPRECATED: Use RecoveryDaemon.scan_and_replay() instead.
        Phase 11.2 ownership consolidation.
        """
        logger.warning("RecoveryService.recover_zombie_jobs is DEPRECATED and bypassed. Use RecoveryDaemon.")
        return None

    async def send_with_backoff(
        self,
        phone: str,
        response: ContractResponse | dict,
        retries: int = 5,
        wa_id: Optional[str] = None,
        ignore_guard: bool = False,
    ) -> DeliveryResult:
        """
        WhatsApp Delivery with Exponential Backoff + Jitter.
        Ensures no message is lost after execution commit.
        """
        from app.services.whatsapp_service import send_response
        
        base_delay = 1 # second
        for attempt in range(retries):
            try:
                await send_response(phone, response, ignore_guard=ignore_guard, wa_id=wa_id)
                logger.info(f"WhatsApp message delivered to {phone} (attempt={attempt+1}, wa_id={wa_id})")
                return DeliveryResult.DELIVERED
            except Exception as e:
                if _extract_whatsapp_error_code(e) == 131030:
                    logger.warning(
                        "Sandbox allowlist block detected for %s (wa_id=%s). Suppressing retries.",
                        phone,
                        wa_id,
                    )
                    return DeliveryResult.SKIPPED_SANDBOX
                logger.error(f"WhatsApp delivery failed attempt {attempt+1}: {e}")
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)
        
        logger.error(f"CRITICAL: Failed to deliver message to {phone} after {retries} retries.")
        return DeliveryResult.FAILED
