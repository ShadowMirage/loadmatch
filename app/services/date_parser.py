import dateparser
from datetime import datetime, timedelta

def normalize_date(date_str: str) -> str:
    """
    Normalizes natural language date strings (e.g., 'tomorrow') to DD-MM-YYYY format.
    If no date is found, returns the original string.
    """
    if not date_str:
        return ""
    
    # Clean string
    cleaned = date_str.lower().strip()
    
    # Use dateparser with relative base
    # settings PREFER_DATES_FROM = 'future' is useful for a logistics app
    settings = {
        'PREFER_DATES_FROM': 'future',
        'RETURN_AS_TIMEZONE_AWARE': False
    }
    
    parsed = dateparser.parse(cleaned, settings=settings)
    
    if parsed:
        return parsed.strftime("%d-%m-%Y")
    
    return ""
