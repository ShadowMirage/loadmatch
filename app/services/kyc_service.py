import logging
from uuid import uuid4
from sqlalchemy.orm import Session

from app.models.kyc import KycDocument
from app.models.enums import DocType
from app.services.storage_service import upload_whatsapp_media
from app.services.whatsapp_service import send_text

logger = logging.getLogger(__name__)


async def handle_kyc_image(
    user_id,
    phone: str,
    media_id: str,
    db: Session,
    doc_type: DocType = DocType.aadhaar,
) -> None:
    """
    Downloads a WhatsApp media image, uploads it to S3, stores
    a KycDocument record, and sends a confirmation message to the user.

    doc_type defaults to aadhaar (the first step in the KYC flow).
    Callers can pass a different DocType if the session makes it clear.
    """
    logger.info("Handling KYC image upload for user_id=%s media_id=%s", user_id, media_id)

    try:
        # 1. Download from WhatsApp and upload to S3
        file_url = await upload_whatsapp_media(media_id)
        logger.info("KYC image uploaded to S3: %s", file_url)

        # 2. Create DB record
        doc = KycDocument(
            user_id=user_id,
            doc_type=doc_type,
            file_url=file_url,
            verified=False,
        )
        db.add(doc)
        db.commit()
        logger.info("KycDocument record created for user_id=%s", user_id)

        # 3. Confirm to user
        await send_text(phone, "✅ KYC document received. Our team will review it shortly.")

    except Exception as e:
        logger.error("KYC upload failed for user_id=%s: %s", user_id, e)
        msg = (
            "⚠️ Unable to read document\n\n"
            "Please upload a clear image of:\n"
            "• Aadhaar front side\n"
            "• Good lighting\n"
            "• Full card visible"
        )
        from app.services.whatsapp_service import send_interactive_buttons
        await send_interactive_buttons(phone, msg, [{"id": "MAIN_MENU", "title": "🏠 Main Menu"}])
        return False
