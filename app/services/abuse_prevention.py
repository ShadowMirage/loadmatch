import time

# Simple in-memory tracker for rate limiting and duplicate detection
_user_message_history = {}

def detect_spam(user_id: str, input_text: str) -> bool:
    """
    Returns True if spam behavior is detected:
    - >5 messages in 10 sec
    - identical repeated input
    - invalid values (e.g. 0 kg)
    """
    if not input_text:
        return False
        
    now = time.time()
    history = _user_message_history.get(user_id, {"timestamps": [], "last_text": ""})
    cleaned_input = input_text.strip().lower()

    # Rule 3: Invalid nonsense values
    if cleaned_input in ["0 kg", "0kg", "0", "test", "nonsense"]:
        return True
        
    # Ensure history structure has repeat_count
    if "repeat_count" not in history:
        history["repeat_count"] = 0

    # Rule 2: Identical repeated input
    if input_text == history["last_text"] and len(cleaned_input) > 4:
        history["repeat_count"] += 1
        history["timestamps"].append(now)
        _user_message_history[user_id] = history
        if history["repeat_count"] >= 2: # 1st msg = 0 repeats, 2nd = 1, 3rd = 2 returns True
            return True
    else:
        history["repeat_count"] = 0
        
    # Rule 1: > 5 messages in 10 seconds
    valid_times = [t for t in history["timestamps"] if now - t <= 10]
    valid_times.append(now)
    
    if len(valid_times) > 5:
        history["timestamps"] = valid_times
        history["last_text"] = input_text
        _user_message_history[user_id] = history
        return True
        
    # Update state
    history["timestamps"] = valid_times
    history["last_text"] = input_text
    _user_message_history[user_id] = history
    
    return False

def clear_spam_history(user_id: str):
    if user_id in _user_message_history:
        del _user_message_history[user_id]
