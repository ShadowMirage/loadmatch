from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str
    WA_TOKEN: str
    WA_PHONE_NUMBER_ID: str
    whatsapp_verify_token: str
    DEV_MODE: bool = False
    ANTHROPIC_API_KEY: str
    AWS_ACCESS_KEY: str
    AWS_SECRET_KEY: str
    AWS_REGION: str
    S3_BUCKET: str
    ADMIN_API_KEY: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding='utf-8', extra="ignore")

settings = Settings()
