import asyncio
import logging
import random
from typing import Optional
from sqlalchemy.orm import Session
from app.contracts.responses import Response as ContractResponse

logger = logging.getLogger(__name__)

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
    ):
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
                return True
            except Exception as e:
                logger.error(f"WhatsApp delivery failed attempt {attempt+1}: {e}")
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)
        
        logger.error(f"CRITICAL: Failed to deliver message to {phone} after {retries} retries.")
        return False
