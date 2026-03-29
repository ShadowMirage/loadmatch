from typing import Optional

from app.config import settings
from app.runtime import environment


def resolve_credentials() -> tuple[Optional[str], Optional[str]]:
    token = settings.WHATSAPP_TOKEN or settings.WA_TOKEN or settings.WHATSAPP_ACCESS_TOKEN
    phone_id = settings.WHATSAPP_PHONE_NUMBER_ID or settings.WA_PHONE_NUMBER_ID
    return token, phone_id


def verify_token() -> Optional[str]:
    return settings.WHATSAPP_VERIFY_TOKEN or settings.whatsapp_verify_token


def delivery_enabled() -> bool:
    if not environment.whatsapp_enabled():
        return False

    token, phone_id = resolve_credentials()
    return not settings.is_placeholder(token) and not settings.is_placeholder(phone_id)
