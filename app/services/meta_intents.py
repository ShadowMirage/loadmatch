import logging

logger = logging.getLogger(__name__)

GREETING_WORDS = {
    "hi",
    "hello",
    "hey",
    "namaste",
    "start",
    "menu"
}

def is_greeting(text: str) -> bool:
    """
    Determines if the message is a meta-intent interrupt (greeting/menu).
    This is used to bypass domain workflow guards.
    """
    if not text:
        return False
    
    # Process the first word for quick meta-intent matching
    token = text.strip().split()[0].lower()
    return token in GREETING_WORDS
