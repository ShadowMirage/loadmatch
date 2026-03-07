import httpx
from app.config import settings

WA_API_URL = f"https://graph.facebook.com/v19.0"

async def send_text(to: str, text: str) -> None:
    url = f"{WA_API_URL}/{settings.WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "body": text
        }
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()

async def download_media(media_id: str) -> bytes:
    headers = {
        "Authorization": f"Bearer {settings.WA_TOKEN}"
    }
    
    async with httpx.AsyncClient() as client:
        # Step 1: Get media URL
        media_url_req = f"{WA_API_URL}/{media_id}"
        media_info_response = await client.get(media_url_req, headers=headers)
        media_info_response.raise_for_status()
        
        media_data = media_info_response.json()
        media_url = media_data.get("url")
        
        if not media_url:
            raise ValueError(f"Could not retrieve URL for media_id: {media_id}")
            
        # Step 2: Download the actual file bytes
        download_response = await client.get(media_url, headers=headers)
        download_response.raise_for_status()
        
        return download_response.content
