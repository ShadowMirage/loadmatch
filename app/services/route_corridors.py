from app.services.logistics_data import normalize_hub_name

_RAW_ROUTE_CORRIDORS = {
    ("jaipur", "delhi"): ["jaipur", "shahpura", "behror", "neemrana", "bhiwadi", "gurugram", "delhi"],
    ("ahmedabad", "mumbai"): ["ahmedabad", "nadiad", "anand", "vadodara", "bharuch", "surat", "navsari", "valsad", "vapi", "palghar", "mumbai"],
    # Add more predefined corridors as the platform scales
}

ROUTE_CORRIDORS = {
    (normalize_hub_name(origin), normalize_hub_name(destination)): [normalize_hub_name(stop) for stop in corridor]
    for (origin, destination), corridor in _RAW_ROUTE_CORRIDORS.items()
}

# Module-level cache to prevent recomputations
route_corridors_cache = {}


def _route_key(origin: str, destination: str) -> tuple[str, str]:
    return normalize_hub_name(origin), normalize_hub_name(destination)


def get_corridor(origin: str, destination: str) -> list[str] | None:
    key = _route_key(origin, destination)
    
    if key in route_corridors_cache:
        return route_corridors_cache[key]
        
    if key in ROUTE_CORRIDORS:
        route_corridors_cache[key] = ROUTE_CORRIDORS[key]
        return route_corridors_cache[key]
        
    # Reverse lookup fallback
    reverse_key = (key[1], key[0])
    if reverse_key in ROUTE_CORRIDORS:
        route_corridors_cache[key] = list(reversed(ROUTE_CORRIDORS[reverse_key]))
        return route_corridors_cache[key]
        
    return None

def is_in_corridor(load_pickup: str, load_drop: str, truck_origin: str, truck_dest: str) -> bool:
    """
    Checks if a load's pickup and drop fall sequentially along a truck's route corridor.
    """
    load_pickup_norm = normalize_hub_name(load_pickup)
    load_drop_norm = normalize_hub_name(load_drop)
    
    # Check exact match first (which is a valid 2-stop corridor)
    if load_pickup_norm == normalize_hub_name(truck_origin) and load_drop_norm == normalize_hub_name(truck_dest):
        return True
        
    corridor = get_corridor(truck_origin, truck_dest)
    if not corridor:
        return False
        
    if load_pickup_norm in corridor and load_drop_norm in corridor:
        pickup_idx = corridor.index(load_pickup_norm)
        drop_idx = corridor.index(load_drop_norm)
        return pickup_idx < drop_idx
        
    return False

def get_nearby_routes(origin: str, destination: str) -> list[tuple[str, str]]:
    """
    Suggest nearby route corridors when no exact match exists.
    Used for marketplace liquidity when a perfect match is unavailable.
    """
    origin_norm, dest_norm = _route_key(origin, destination)

    suggestions = []
    seen = set()

    for (corr_origin, corr_dest), corridor in ROUTE_CORRIDORS.items():
        if origin_norm in corridor or dest_norm in corridor:
            route = (corr_origin.title(), corr_dest.title())
            if route not in seen:
                suggestions.append(route)
                seen.add(route)

    return suggestions[:3]
