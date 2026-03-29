import logging
import os
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.runtime import environment

logger = logging.getLogger(__name__)

_client = None


def _resolve_access_key() -> str | None:
    return settings.AWS_ACCESS_KEY or os.getenv("AWS_ACCESS_KEY_ID")


def _resolve_secret_key() -> str | None:
    return settings.AWS_SECRET_KEY or os.getenv("AWS_SECRET_ACCESS_KEY")


def get_client():
    global _client

    if _client is not None:
        return _client

    if not environment.s3_enabled():
        return None

    access_key = _resolve_access_key()
    secret_key = _resolve_secret_key()
    bucket = settings.S3_BUCKET or os.getenv("S3_BUCKET")
    if any(settings.is_placeholder(value) for value in (access_key, secret_key, bucket)):
        return None

    _client = boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=settings.AWS_REGION,
    )
    return _client


def store_local_media(content: bytes, filename: str) -> str:
    target_dir = Path(settings.LOCAL_MEDIA_DIR).expanduser() / "kyc"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    target_path.write_bytes(content)
    return str(target_path.resolve())


def upload_bytes(content: bytes, filename: str, content_type: str = "image/jpeg") -> str:
    key = f"kyc/{filename}"
    client = get_client()
    if client is None:
        return store_local_media(content, filename)

    try:
        client.put_object(
            Bucket=settings.S3_BUCKET or os.getenv("S3_BUCKET"),
            Key=key,
            Body=content,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError, OSError, RuntimeError) as exc:
        logger.warning("S3 upload unavailable. Falling back to local media storage. Error=%s", exc)
        return store_local_media(content, filename)

    bucket = settings.S3_BUCKET or os.getenv("S3_BUCKET")
    return f"https://{bucket}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"

