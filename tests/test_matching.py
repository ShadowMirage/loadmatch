import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.user import User, UserRole, KycStatus
from app.models.truck import Truck, TruckType
from app.models.listing import TruckSpaceListing, ListingStatus
from app.models.load_request import LoadRequest, LoadRequestStatus
from app.services.matching_service import find_matches_for_load

def run_test():
    # Setup in-memory SQLite database
    print("Setting up in-memory database...")
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    
    # 1. Create a transporter user
    transporter = User(
        phone="919876543210", 
        name="Rahul Transporter", 
        role=UserRole.transporter,
        kyc_status=KycStatus.verified
    )
    db.add(transporter)
    db.flush()
    
    # 2. Add their truck
    truck = Truck(
        owner_id=transporter.id,
        registration_number="MH12AB3456",
        truck_type=TruckType.medium,
        total_capacity_kg=1000
    )
    db.add(truck)
    db.flush()
    
    # 3. Create a listing (Delhi -> Mumbai, 500kg, ₹5/kg, today)
    today = datetime.date.today()
    listing = TruckSpaceListing(
        truck_id=truck.id,
        owner_id=transporter.id,
        from_city="Delhi",
        to_city="Mumbai",
        departure_date=today,
        total_capacity_kg=1000,
        available_capacity_kg=500,
        price_per_kg=5.00,
        status=ListingStatus.open
    )
    db.add(listing)
    
    # 4. Create a shipper user
    shipper = User(
        phone="919988776655",
        name="Amit Shipper",
        role=UserRole.shipper,
        kyc_status=KycStatus.pending
    )
    db.add(shipper)
    db.flush()
    
    # 5. Create a load request (Delhi -> Mumbai, 200kg, today, up to ₹10/kg budget)
    load = LoadRequest(
        shipper_id=shipper.id,
        from_city="Delhi",
        to_city="Mumbai",
        pickup_date=today,
        weight_kg=200,
        budget_per_kg=10.00,
        status=LoadRequestStatus.open
    )
    db.add(load)
    db.commit()
    
    print("\n--- Testing Matchmaking Logic ---")
    print(f"Load: {load.weight_kg}kg | {load.from_city} -> {load.to_city} | {load.pickup_date} | Budget: {load.budget_per_kg}")
    print(f"Listing: {listing.available_capacity_kg}kg | {listing.from_city} -> {listing.to_city} | {listing.departure_date} | Price: {listing.price_per_kg}")
    
    # 6. Call matching service
    matches = find_matches_for_load(db, load)
    
    # Assertions
    assert len(matches) > 0, "No matches were found!"
    top_match = matches[0]
    assert top_match["score"] > 0, "Match score should be > 0"
    
    print("\n✅ MATCH FOUND RESULT:")
    for key, value in top_match.items():
         print(f"   {key}: {value}")
         
    print("\n✅ Test passed successfully!")
    db.close()

if __name__ == "__main__":
    run_test()
