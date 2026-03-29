import os


def _has_env(*names: str) -> bool:
    return any(bool((os.getenv(name) or "").strip()) for name in names)


def redis_enabled() -> bool:
    return _has_env("REDIS_URL")


def postgres_enabled() -> bool:
    return "postgres" in (os.getenv("DATABASE_URL", "") or "").lower()


def s3_enabled() -> bool:
    has_access_key = _has_env("AWS_ACCESS_KEY", "AWS_ACCESS_KEY_ID")
    has_secret_key = _has_env("AWS_SECRET_KEY", "AWS_SECRET_ACCESS_KEY")
    return has_access_key and has_secret_key and _has_env("S3_BUCKET")


def whatsapp_enabled() -> bool:
    has_token = _has_env("WHATSAPP_TOKEN", "WA_TOKEN", "WHATSAPP_ACCESS_TOKEN")
    has_phone_id = _has_env("WHATSAPP_PHONE_NUMBER_ID", "WA_PHONE_NUMBER_ID")
    return has_token and has_phone_id


def anthropic_enabled() -> bool:
    return _has_env("ANTHROPIC_API_KEY")

