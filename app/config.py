from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./var/loadmatch_demo.db"
    LOCAL_DATABASE_URL: str = "sqlite:///./var/loadmatch_demo.db"
    AUTO_FALLBACK_TO_SQLITE: bool = True
    REDIS_URL: str = "redis://redis:6379/0"

    # WhatsApp credentials
    WHATSAPP_TOKEN: Optional[str] = None
    WHATSAPP_PHONE_NUMBER_ID: Optional[str] = None
    WHATSAPP_VERIFY_TOKEN: Optional[str] = None

    # Legacy aliases
    WA_TOKEN: Optional[str] = None
    WA_PHONE_NUMBER_ID: Optional[str] = None
    WHATSAPP_ACCESS_TOKEN: Optional[str] = None
    whatsapp_verify_token: Optional[str] = None

    DEV_MODE: bool = False

    ANTHROPIC_API_KEY: Optional[str] = None
    AWS_ACCESS_KEY: Optional[str] = None
    AWS_SECRET_KEY: Optional[str] = None
    AWS_REGION: str = "us-east-1"
    S3_BUCKET: Optional[str] = None
    LOCAL_MEDIA_DIR: str = "./var/media"
    ADMIN_API_KEY: str = "dev-admin-key"

    AI_FIRST_MODE: bool = False
    MARKETPLACE_DUPLICATE_WINDOW_MINUTES: int = 45

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @staticmethod
    def is_placeholder(value: Optional[str]) -> bool:
        if value is None:
            return True
        normalized = str(value).strip().lower()
        if not normalized:
            return True
        return normalized.startswith("your_") or normalized.endswith("_here")

settings = Settings()
