from rapidfuzz import process, utils, fuzz

# Immutable tuple for faster lookup
MAJOR_CITIES = (
    "delhi", "new delhi", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai", "kolkata",
    "ahmedabad", "pune", "surat", "jaipur", "lucknow", "kanpur", "nagpur", "indore", "thane",
    "bhopal", "visakhapatnam", "patna", "vadodara", "ghaziabad", "ludhiana",
    "agra", "nashik", "faridabad", "meerut", "rajkot", "varanasi", "srinagar",
    "aurangabad", "amritsar", "navi mumbai", "allahabad", "prayagraj",
    "howrah", "ranchi", "gwalior", "jabalpur", "coimbatore", "vijayawada",
    "jodhpur", "madurai", "raipur", "kota", "guwahati", "chandigarh",
    "mysore", "gurgaon", "gurugram", "aligarh", "jalandhar",
    "bhubaneswar", "warangal", "thiruvananthapuram",
    "guntur", "gorakhpur", "bikaner", "noida", "jamshedpur",
    "kochi", "kolhapur", "ajmer", "akola", "jamnagar",
    "ujjain", "siliguri", "jhansi", "jammu", "mangalore",
    "tirunelveli", "gaya", "jalgaon", "udaipur",
    "kozhikode", "kurnool", "rajahmundry", "bokaro",
    "bellary", "patiala", "agartala", "bhagalpur",
    "muzaffarnagar", "latur", "dhule", "rohtak",
    "sagar", "bhilwara", "muzaffarpur", "ahmednagar",
    "mathura", "kollam", "kadapa", "sambalpur",
    "bilaspur", "satara", "kakinada", "shimoga",
    "chandrapur", "junagadh", "thrissur", "alwar",
    "nizamabad", "tumkur", "khammam", "panvel",
    "darbhanga", "aizawl", "dewas", "karnal",
    "bathinda", "jalna", "eluru", "satna",
    "ratlam", "hapur", "arrah", "anantapur",
    "karimnagar", "etawah", "ambernath", "bharatpur",
    "begusarai", "gandhidham", "puducherry", "sikar",
    "thoothukudi", "mirzapur", "raichur", "pali",
    "ramagundam", "haridwar", "vijayanagaram",
    "katihar", "nagercoil", "shivamogga",
    "bulandshahr", "panchkula", "ambala",
    "fatehpur", "thanjavur", "vapi"
)

# Aliases for consistent marketplace routing
CITY_ALIASES = {
    "new delhi": "delhi",
    "bangalore": "bengaluru",
    "gurgaon": "gurugram",
    "allahabad": "prayagraj",
}

def normalize_city(city_name: str) -> str:
    """
    Normalize city names using fuzzy matching.

    Returns a standardized city name if confidence is high,
    otherwise returns the cleaned original.
    """

    if not city_name:
        return ""

    # Prevent long sentences entering fuzzy matcher
    if len(city_name) > 40:
        return city_name.strip().lower()

    clean_name = utils.default_process(city_name)

    if not clean_name:
        return city_name.strip().lower()

    # Rapid fuzzy lookup
    match = process.extractOne(
        clean_name,
        MAJOR_CITIES,
        scorer=fuzz.WRatio,
        score_cutoff=85
    )

    if match:
        standard_city = match[0]

        # Apply alias normalization
        if standard_city in CITY_ALIASES:
            return CITY_ALIASES[standard_city]

        return standard_city

    return clean_name