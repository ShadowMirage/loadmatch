import re

def normalize_weight(text: str) -> int:
    """
    Converts natural language weight strings to integer kilograms.
    Examples:
    '3 ton' -> 3000
    '3 tons 500kg' -> 3500
    '200kg' -> 200
    '3.5 tons' -> 3500
    """
    if not text:
        return 0
        
    text = text.lower().strip()
    
    total_kg = 0
    
    # 1. Look for Tons (t, ton, tons)
    # Handles 3 ton, 3.5 tons, 3t
    ton_match = re.search(r"(\d+\.?\d*)\s*(t|ton|tons)", text)
    if ton_match:
        tons = float(ton_match.group(1))
        total_kg += int(tons * 1000)
        # Remove the ton part from text to avoid double counting kg if they mention both
        text = text.replace(ton_match.group(0), "")
        
    # 2. Look for Kilograms (kg, kgs, kilo, kilogram, kilograms)
    kg_match = re.search(r"(\d+\.?\d*)\s*(kg|kgs|kilo|kilogram|kilograms)", text)
    if kg_match:
        kgs = float(kg_match.group(1))
        total_kg += int(kgs)
    elif not total_kg:
        # 3. Fallback: if no units found but there's a number, assume kg
        digits = re.search(r"(\d+)", text)
        if digits:
            total_kg = int(digits.group(1))
            
    return int(total_kg)
