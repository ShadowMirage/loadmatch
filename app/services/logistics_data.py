import logging
from typing import Optional, Dict, Set, List
from enum import Enum
from rapidfuzz import process, utils, fuzz

logger = logging.getLogger(__name__)

class CorridorSource(str, Enum):
    CITY_PAIR = "city_pair"
    INDUSTRIAL_ZONE_PAIR = "industrial_zone_pair"
    ALIAS_PAIR = "alias_pair"
    ADJACENT_CITY_PAIR = "adjacent_city_pair"

class LaneClass(str, Enum):
    """Routing tier for a city-pair corridor. Drives pricing, SLA, and supply prioritization."""
    TIER1_SPINE      = "tier1_spine"       # Both cities are Tier-1 metros
    INDUSTRIAL_CLUSTER = "industrial_cluster" # At least one end is an industrial zone / belt
    REGIONAL_LANE    = "regional_lane"     # Mixed: one Tier-1 + regional hub
    SHORTHAUL_LANE   = "shorthaul_lane"    # Both cities within the same regional belt

# Metadata for versioning/traceability
RESOLVER_VERSION = "v3_integrity_cleanup"

# Categorized by logistics/industrial belts for easier maintenance
HUB_LAYERS = {
    "tier1": {"delhi", "mumbai", "chennai", "bangalore", "hyderabad", "kolkata", "ahmedabad", "pune", "surat", "jaipur", "lucknow", "indore", "nagpur", "kanpur"},
    "ncr_satellites": {"gurgaon", "manesar", "bhiwadi", "neemrana", "sonipat", "panipat", "faridabad", "ghaziabad", "noida", "greater noida", "meerut", "jhajjar", "rohtak"},
    "gujarat_spine": {"sanand", "vadodara", "baroda", "rajkot", "vapi", "ankleshwar", "bharuch", "morbi", "kandla", "mundra", "hazira"},
    "south_triangle": {"sriperumbudur", "hosur", "tiruppur", "salem", "coimbatore", "trichy", "madurai"},
    "punjab_loop": {"ludhiana", "amritsar", "jalandhar", "mohali", "zirakpur", "chandigarh"},
    "east_belt": {"durgapur", "asansol", "howrah", "patna", "ranchi", "raipur"},
    "andhra_vizag": {"vizag", "vijayawada", "guntur", "nellore", "kakinada"},
    "kerala_western": {"kochi", "thrissur", "calicut"}
}

CITY_LOGISTICS_HUBS: Set[str] = set().union(*HUB_LAYERS.values())

# Comprehensive city list for fuzzy fallback (formerly MAJOR_CITIES)
ALL_RECOGNIZED_CITIES = list(CITY_LOGISTICS_HUBS) + [
    "visakhapatnam", "ghaziabad", "agra", "nashik", "varanasi", "srinagar", "aurangabad", "navi mumbai",
    "prayagraj", "gwalior", "jabalpur", "bhopal", "guwahati", "kota", "mysore", "jamshedpur", "jodhpur",
    "ajmer", "jammu", "mangalore", "udaipur", "jhansi", "siliguri", "agartala", "bhagalpur", "latur",
    "muzaffarpur", "mathura", "kollam", "bilaspur", "satara", "shimoga", "alwar", "panvel", "aizawl",
    "puducherry", "haridwar", "shivamogga", "ambala", "fatehpur", "thanjavur"
]

INDUSTRIAL_ZONE_ALIASES: Dict[str, str] = {
    "sanand gidc": "sanand", "ankleshwar gidc": "ankleshwar", "vapi gidc": "vapi",
    "manesar imt": "manesar", "bhiwadi industrial": "bhiwadi"
}

CITY_ALIASES: Dict[str, str] = {
    # Transport/Operator Shorthand
    "dilli": "delhi", "blr": "bangalore", "mum": "mumbai", "hyd": "hyderabad", 
    "vzg": "vizag", "amd": "ahmedabad", "baroda": "vadodara",
    # Name standardizations
    "bengaluru": "bangalore", # 🚨 Marketplace uses bangalore (not bengaluru)
    "gurugram": "gurgaon",
    "banglore": "bangalore", "ahmdabad": "ahmedabad",
    "new delhi": "delhi", "ncr": "delhi", "delhi ncr": "delhi",
    "vizag": "visakhapatnam", "bombay": "mumbai", "allahabad": "prayagraj",
    # Map Industrial Zones too
    **INDUSTRIAL_ZONE_ALIASES
}

ROUTE_CONNECTORS: Set[str] = {"to", "se", "tak", "se lekar", "-", "/"}

def normalize_hub_name(token: str) -> Optional[str]:
    """
    Production-grade hub normalization.
    Order: 1. Direct Alias -> 2. Hub List -> 3. Fuzzy Match -> 4. Fallback.
    Returns: Canonical name or None if confidence too low.
    """
    if not token or len(token) > 40:
        return None
        
    t = token.lower().strip()
    
    # 1. Alias Resolution (High priority)
    if t in CITY_ALIASES:
        return CITY_ALIASES[t]
        
    # 2. Existing Hub Check (High precision)
    if t in CITY_LOGISTICS_HUBS:
        return t
        
    # 3. Fuzzy Lookup (Typo tolerance)
    clean_name = utils.default_process(t)
    # Require at least 4 characters for fuzzy match to prevent stop-word false positives (e.g., "go" matching "gaya")
    if not clean_name or len(clean_name) < 4:
        return None
        
    match = process.extractOne(
        clean_name,
        ALL_RECOGNIZED_CITIES,
        scorer=fuzz.WRatio,
        score_cutoff=85
    )
    
    if match:
        standard_city = match[0]
        # Recursively alias normalized city in case of name change (e.g. prayagraj -> allahabad if needed)
        return CITY_ALIASES.get(standard_city, standard_city)
    
    return None

def get_corridor_source(origin_token: str, dest_token: str) -> CorridorSource:
    """Triage the source of the corridor based on how cities were matched."""
    o_raw = origin_token.lower().strip()
    d_raw = dest_token.lower().strip()
    
    if o_raw in INDUSTRIAL_ZONE_ALIASES or d_raw in INDUSTRIAL_ZONE_ALIASES:
        return CorridorSource.INDUSTRIAL_ZONE_PAIR
    if o_raw in CITY_LOGISTICS_HUBS and d_raw in CITY_LOGISTICS_HUBS:
        return CorridorSource.CITY_PAIR
    if o_raw in CITY_ALIASES or d_raw in CITY_ALIASES:
        return CorridorSource.ALIAS_PAIR
    return CorridorSource.CITY_PAIR

def is_corridor_route(origin: Optional[str], destination: Optional[str]) -> bool:
    """Verifies if a route connects two recognized logistics hubs."""
    if not origin or not destination:
        return False
    return normalize_hub_name(origin) is not None and normalize_hub_name(destination) is not None

# Reverse lookup: city -> hub layer name
_CITY_TO_LAYER: Dict[str, str] = {
    city: layer
    for layer, cities in HUB_LAYERS.items()
    for city in cities
}

def get_lane_class(city_from: str, city_to: str) -> LaneClass:
    """
    Classify a corridor into a routing tier using the HUB_LAYERS taxonomy.

    Logic:
        tier1_spine       — both ends are Tier-1 metros
        industrial_cluster — either end is an industrial belt (gujarat_spine, ncr_satellites, south_triangle, etc.)
        shorthaul_lane    — both ends belong to the same regional belt
        regional_lane     — any other recognized hub combination
    """
    INDUSTRIAL_LAYERS = {"gujarat_spine", "ncr_satellites", "south_triangle", "punjab_loop", "east_belt", "andhra_vizag", "kerala_western"}

    layer_from = _CITY_TO_LAYER.get(city_from)
    layer_to   = _CITY_TO_LAYER.get(city_to)

    if layer_from == "tier1" and layer_to == "tier1":
        return LaneClass.TIER1_SPINE

    if layer_from in INDUSTRIAL_LAYERS or layer_to in INDUSTRIAL_LAYERS:
        return LaneClass.INDUSTRIAL_CLUSTER

    if layer_from and layer_from == layer_to:
        return LaneClass.SHORTHAUL_LANE

    return LaneClass.REGIONAL_LANE
