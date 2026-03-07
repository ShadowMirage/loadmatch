import datetime
from sqlalchemy.orm import Session

from app.models.user import User, UserRole, KycStatus
from app.models.load_request import LoadRequest, LoadRequestStatus
from app.models.truck import Truck, TruckType
from app.models.listing import TruckSpaceListing, ListingStatus
from app.models.match import Match, MatchStatus
from app.models.kyc import KycDocument, DocType
from app.services.matching_service import find_matches_for_load, update_listing_capacity_after_match
from app.services.storage_service import upload_whatsapp_media

TOOLS = [
    {
        "name": "set_user_profile",
        "description": "Save user name and role during first-time onboarding",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The user's full name"},
                "role": {"type": "string", "enum": ["shipper", "transporter", "both"], "description": "The target role setting for the user"}
            },
            "required": ["name", "role"]
        }
    },
    {
        "name": "create_load_request",
        "description": "Create a shipment request — user wants to send goods from one city to another",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_city": {"type": "string", "description": "Origin city"},
                "to_city": {"type": "string", "description": "Destination city"},
                "pickup_date": {"type": "string", "description": "Pickup date as YYYY-MM-DD"},
                "weight_kg": {"type": "integer", "description": "Total weight of the goods in kg"},
                "goods_type": {"type": "string", "description": "Description of the goods"},
                "budget_per_kg": {"type": "number", "description": "Maximum budget per kg"}
            },
            "required": ["from_city", "to_city", "pickup_date", "weight_kg"]
        }
    },
    {
        "name": "create_truck_listing",
        "description": "Post available truck space — transporter has empty/partial truck going somewhere",
        "input_schema": {
            "type": "object",
            "properties": {
                "registration_number": {"type": "string", "description": "Truck registration/license plate number"},
                "from_city": {"type": "string", "description": "Origin city"},
                "to_city": {"type": "string", "description": "Destination city"},
                "departure_date": {"type": "string", "description": "Departure date as YYYY-MM-DD"},
                "available_capacity_kg": {"type": "integer", "description": "Available capacity in kg"},
                "price_per_kg": {"type": "number", "description": "Price per kg for the available space"}
            },
            "required": ["registration_number", "from_city", "to_city", "departure_date", "available_capacity_kg", "price_per_kg"]
        }
    },
    {
        "name": "get_my_loads",
        "description": "Show user their active load requests",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_my_listings",
        "description": "Show transporter their active truck space listings",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "find_matches",
        "description": "Find matching trucks for a specific load request",
        "input_schema": {
            "type": "object",
            "properties": {
                "load_request_id": {"type": "string", "description": "The ID of the load request to match"}
            },
            "required": ["load_request_id"]
        }
    },
    {
        "name": "accept_match",
        "description": "Accept a suggested match between a load and a truck",
        "input_schema": {
            "type": "object",
            "properties": {
                "match_id": {"type": "string", "description": "The ID of the match to accept"}
            },
            "required": ["match_id"]
        }
    },
    {
        "name": "save_kyc_document",
        "description": "Save a KYC document image uploaded by the user via WhatsApp",
        "input_schema": {
            "type": "object",
            "properties": {
                "media_id": {"type": "string", "description": "The WhatsApp media ID string"},
                "doc_type": {"type": "string", "enum": ["aadhaar", "pan", "rc_book", "driving_license", "gst"], "description": "The type of document"}
            },
            "required": ["media_id", "doc_type"]
        }
    }
]

async def execute_tool(name: str, args: dict, db: Session, user: User) -> dict:
    if name == "set_user_profile":
        user.name = args["name"]
        user.role = UserRole(args["role"])
        db.commit()
        return {"status": "success", "message": f"Profile updated. Name: {user.name}, Role: {user.role.value}"}

    elif name == "create_load_request":
        pickup_date = datetime.datetime.strptime(args["pickup_date"], "%Y-%m-%d").date()
        load = LoadRequest(
            shipper_id=user.id,
            from_city=args["from_city"],
            to_city=args["to_city"],
            pickup_date=pickup_date,
            weight_kg=args["weight_kg"],
            goods_type=args.get("goods_type"),
            budget_per_kg=args.get("budget_per_kg")
        )
        db.add(load)
        db.commit()
        db.refresh(load)
        
        matches = find_matches_for_load(db, load)
        
        return {
            "status": "success", 
            "load_request_id": str(load.id), 
            "matches_found": len(matches)
        }

    elif name == "create_truck_listing":
        registration_number = args["registration_number"]
        truck = db.query(Truck).filter(
            Truck.registration_number == registration_number,
            Truck.owner_id == user.id
        ).first()
        
        if not truck:
            truck = Truck(
                owner_id=user.id,
                registration_number=registration_number,
                truck_type=TruckType.medium, # Default assuming not asked
                total_capacity_kg=args["available_capacity_kg"] # Default starting point
            )
            db.add(truck)
            db.flush()
            
        departure_date = datetime.datetime.strptime(args["departure_date"], "%Y-%m-%d").date()
        listing = TruckSpaceListing(
            truck_id=truck.id,
            owner_id=user.id,
            from_city=args["from_city"],
            to_city=args["to_city"],
            departure_date=departure_date,
            total_capacity_kg=truck.total_capacity_kg,
            available_capacity_kg=args["available_capacity_kg"],
            price_per_kg=args["price_per_kg"]
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)
        
        return {
            "status": "success",
            "listing_id": str(listing.id)
        }

    elif name == "get_my_loads":
        loads = db.query(LoadRequest).filter(
            LoadRequest.shipper_id == user.id,
            LoadRequest.status.in_([LoadRequestStatus.open, LoadRequestStatus.matched])
        ).all()
        
        return [
            {
                "id": str(load.id),
                "from_city": load.from_city,
                "to_city": load.to_city,
                "pickup_date": load.pickup_date.isoformat(),
                "weight_kg": load.weight_kg,
                "status": load.status.value
            } for load in loads
        ]

    elif name == "get_my_listings":
        listings = db.query(TruckSpaceListing).filter(
            TruckSpaceListing.owner_id == user.id,
            TruckSpaceListing.status.in_([ListingStatus.open, ListingStatus.partial])
        ).all()
        
        return [
            {
                "id": str(listing.id),
                "from_city": listing.from_city,
                "to_city": listing.to_city,
                "departure_date": listing.departure_date.isoformat(),
                "available_capacity_kg": listing.available_capacity_kg,
                "price_per_kg": float(listing.price_per_kg),
                "status": listing.status.value
            } for listing in listings
        ]

    elif name == "find_matches":
        load = db.query(LoadRequest).filter(LoadRequest.id == args["load_request_id"]).first()
        if not load:
            return {"error": "Load request not found"}
            
        matches = find_matches_for_load(db, load)
        load.status = LoadRequestStatus.matched
        db.commit()
        
        return matches

    elif name == "accept_match":
        match = db.query(Match).filter(Match.id == args["match_id"]).first()
        if not match:
            return {"error": "Match not found"}
            
        load = db.query(LoadRequest).filter(LoadRequest.id == match.load_request_id).first()
        listing = db.query(TruckSpaceListing).filter(TruckSpaceListing.id == match.listing_id).first()
        
        if load.shipper_id == user.id:
            match.shipper_confirmed = True
        elif listing.owner_id == user.id:
            match.transporter_confirmed = True
        else:
            return {"error": "User unauthorized or not party to this match"}
            
        if match.shipper_confirmed and match.transporter_confirmed:
            update_listing_capacity_after_match(db, str(match.id))
            return {"status": "success", "message": "Match fully accepted and confirmed by both parties", "match_status": match.status.value}
            
        db.commit()
        
        return {
            "status": "success", 
            "message": "Match accepted. Waiting for other party to confirm.",
            "match_status": match.status.value
        }

    elif name == "save_kyc_document":
        try:
            file_url = await upload_whatsapp_media(args["media_id"])
            kyc_doc = KycDocument(
                user_id=user.id,
                doc_type=DocType(args["doc_type"]),
                file_url=file_url
            )
            
            db.add(kyc_doc)
            user.kyc_status = KycStatus.under_review
            db.commit()
            
            return {
                "status": "success",
                "message": "Document received, team will verify in 24 hours"
            }
        except Exception as e:
            return {"error": str(e)}

    else:
        return {"error": f"Unknown tool: {name}"}
