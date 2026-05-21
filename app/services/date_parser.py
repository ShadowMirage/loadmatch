from datetime import datetime, timedelta, timezone
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


def parse_message_timestamp(ts_val: str | int) -> datetime:
    """
    Parses WhatsApp/Meta message timestamp (epoch seconds or ms)
    into a timezone-aware Asia/Kolkata datetime.
    """
    try:
        ts = float(ts_val)
        # Meta timestamps can be 10 digits (seconds) or 13 digits (ms)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(USER_TIMEZONE)
    except Exception:
        return _relative_base_now()


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


def normalize_date(date_str: str, *, relative_base: datetime) -> str:
    """
    Normalizes natural language date strings (e.g., 'tomorrow') to DD-MM-YYYY format.
    If no date is found, returns an empty string.
    """
    if not date_str:
        return ""

    cleaned = str(date_str).strip()
    if not cleaned:
        return ""

    explicit_relative = _normalize_relative_phrase(cleaned, relative_base)
    if explicit_relative:
        return explicit_relative

    parsed = dateparser.parse(cleaned.lower(), settings=_dateparser_settings(relative_base))
    if parsed:
        return parsed.astimezone(USER_TIMEZONE).strftime("%d-%m-%Y")
    return ""


def extract_first_date(text: str, *, relative_base: datetime) -> str:
    if not text:
        return ""

    cleaned = str(text).strip()
    if not cleaned:
        return ""

    explicit_relative = _normalize_relative_phrase(cleaned, relative_base)
    if explicit_relative:
        return explicit_relative

    matches = search_dates(
        cleaned,
        settings=_dateparser_settings(relative_base),
        languages=["en"],
    )

    if not matches:
        return ""

    _, parsed = matches[0]
    return parsed.astimezone(USER_TIMEZONE).strftime("%d-%m-%Y")
