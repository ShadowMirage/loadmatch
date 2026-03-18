def normalize_city(city: str) -> str:
    """Normalize city names to a standard format for better matching."""
    if not city:
        return ""
        
    city = city.strip().upper()
    
    # Mappings for common India logistics routes and their variants
    mappings = {
        "NEW DELHI": "DELHI",
        "NCR": "DELHI",
        "BOMBAY": "MUMBAI",
        "BENGALURU": "BANGALORE",
        "CALCUTTA": "KOLKATA",
        "MADRAS": "CHENNAI",
        "GURGAON": "GURUGRAM",
        "POONA": "PUNE",
        "TRIVANDRUM": "THIRUVANANTHAPURAM",
        "BARODA": "VADODARA"
    }
    
    for key, val in mappings.items():
        if city == key:
            return val
            
    return city
