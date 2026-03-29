import uuid

from app.runtime.storage_adapter import upload_bytes
from app.services import whatsapp_service

def upload_media_bytes(content: bytes, filename: str) -> str:
    return upload_bytes(content, filename, content_type="image/jpeg")

async def upload_whatsapp_media(media_id: str) -> str:
    # Get bytes from WhatsApp
    content = await whatsapp_service.download_media(media_id)
    
    # Generate unique filename
    filename = f"{uuid.uuid4()}.jpg"
    
    # Upload and return URL
    return upload_media_bytes(content, filename)
