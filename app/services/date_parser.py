from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates

USER_TIMEZONE = ZoneInfo("Asia/Kolkata")
_RELATIVE_DATE_OFFSETS = (
    ("day after tomorrow", 2),
    ("tomorrow", 1),
    ("today", 0),
)


def _relative_base_now() -> datetime:
    return datetime.now(USER_TIMEZONE)


def _dateparser_settings(relative_base: datetime) -> dict:
    return {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": "Asia/Kolkata",
        "TO_TIMEZONE": "Asia/Kolkata",
        "RELATIVE_BASE": relative_base,
    }


def _normalize_relative_phrase(text: str, relative_base: datetime) -> str:
    lowered = text.lower().strip()
    for phrase, day_offset in _RELATIVE_DATE_OFFSETS:
        if phrase in lowered:
            return (relative_base.date() + timedelta(days=day_offset)).strftime("%d-%m-%Y")
    return ""


def normalize_date(date_str: str, *, relative_base: datetime | None = None) -> str:
    """
    Normalizes natural language date strings (e.g., 'tomorrow') to DD-MM-YYYY format.
    If no date is found, returns an empty string.
    """
    if not date_str:
        return ""

    cleaned = str(date_str).strip()
    if not cleaned:
        return ""

    local_base = relative_base or _relative_base_now()
    explicit_relative = _normalize_relative_phrase(cleaned, local_base)
    if explicit_relative:
        return explicit_relative

    parsed = dateparser.parse(cleaned.lower(), settings=_dateparser_settings(local_base))
    if parsed:
        return parsed.astimezone(USER_TIMEZONE).strftime("%d-%m-%Y")
    return ""


def extract_first_date(text: str, *, relative_base: datetime | None = None) -> str:
    if not text:
        return ""

    cleaned = str(text).strip()
    if not cleaned:
        return ""

    local_base = relative_base or _relative_base_now()
    explicit_relative = _normalize_relative_phrase(cleaned, local_base)
    if explicit_relative:
        return explicit_relative

    matches = search_dates(
        cleaned,
        settings=_dateparser_settings(local_base),
        languages=["en"],
    )
    if not matches:
        return ""

    _, parsed = matches[0]
    return parsed.astimezone(USER_TIMEZONE).strftime("%d-%m-%Y")
