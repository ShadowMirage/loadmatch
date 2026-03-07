import uuid
import boto3
from app.config import settings
from app.services.whatsapp_service import download_media

s3_client = boto3.client(
    "s3",
    aws_access_key_id=settings.AWS_ACCESS_KEY,
    aws_secret_access_key=settings.AWS_SECRET_KEY,
    region_name=settings.AWS_REGION
)

def upload_media_bytes(content: bytes, filename: str) -> str:
    key = f"kyc/{filename}"
    s3_client.put_object(
        Bucket=settings.S3_BUCKET,
        Key=key,
        Body=content,
        ContentType="image/jpeg"
    )
    
    return f"https://{settings.S3_BUCKET}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"

async def upload_whatsapp_media(media_id: str) -> str:
    # Get bytes from WhatsApp
    content = await download_media(media_id)
    
    # Generate unique filename
    filename = f"{uuid.uuid4()}.jpg"
    
    # Upload and return URL
    return upload_media_bytes(content, filename)
