from enum import Enum

class UserRole(str, Enum):
    shipper = "shipper"
    transporter = "transporter"
    both = "both"

class CargoCategory(str, Enum):
    agriculture = "agriculture"
    food = "food"
    steel = "steel"
    cement = "cement"
    chemicals = "chemicals"
    machinery = "machinery"
    textiles = "textiles"
    electronics = "electronics"
    consumer_goods = "consumer_goods"
    fragile = "fragile"
    hazardous = "hazardous"

class KycFlowState(str, Enum):
    not_started = "not_started"
    aadhaar_uploaded = "aadhaar_uploaded"
    pan_uploaded = "pan_uploaded"
    rc_uploaded = "rc_uploaded"
    license_uploaded = "license_uploaded"
    under_review = "under_review"
    verified = "verified"
    rejected = "rejected"

class MatchStatus(str, Enum):
    suggested = "suggested"
    pending = "pending"
    accepted = "accepted"
    confirmed = "confirmed"
    in_transit = "in_transit"
    delivered = "delivered"
    completed = "completed"
    cancelled = "cancelled"
    expired = "expired"

class DocType(str, Enum):
    aadhaar = "aadhaar"
    pan = "pan"
    rc_book = "rc_book"
    driving_license = "driving_license"
    gst = "gst"

class LoadRequestStatus(str, Enum):
    open = "open"
    matched = "matched"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"

class ListingStatus(str, Enum):
    open = "open"
    partial = "partial"
    full = "full"
    completed = "completed"
    cancelled = "cancelled"

class TruckType(str, Enum):
    mini = "mini"
    medium = "medium"
    large = "large"
    trailer = "trailer"
