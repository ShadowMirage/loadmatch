import json
from sqlalchemy.orm import Session
from app.models.user import User

def update_user_memory(db: Session, user: User, data: dict):
    """
    Updates the JSON user memory payload with frequency tracks:
    - route frequency (from-to)
    - avg weight
    - cargo type frequency
    """
    if not user:
        return

    # User may have an empty memory dict or string
    memory = user.memory_data if isinstance(user.memory_data, dict) else {}

    # Extract relevant fields
    pickup = data.get("from", "").strip().lower()
    drop = data.get("to", "").strip().lower()
    weight = max(int(data.get("weight_kg") or 0), int(data.get("capacity_kg") or 0))
    cargo = data.get("cargo", "").strip().lower()

    # Track route
    if pickup and drop:
        route_key = f"{pickup}_{drop}"
        routes = memory.setdefault("frequent_routes", {})
        routes[route_key] = routes.get(route_key, 0) + 1
        
    # Track average weight dynamically
    if weight > 0:
        avg_w = memory.get("avg_weight", 0)
        count_w = memory.get("weight_entries", 0)
        memory["avg_weight"] = ((avg_w * count_w) + weight) / (count_w + 1)
        memory["weight_entries"] = count_w + 1

    # Track cargo
    if cargo:
        cargos = memory.setdefault("frequent_cargos", {})
        cargos[cargo] = cargos.get(cargo, 0) + 1

    user.memory_data = memory
    db.commit()


def personalize_response(user: User) -> str:
    """
    Generates an AI persona context block based on user's memory,
    instructing it to prioritize frequent routes if matches are ambiguous.
    """
    if not user or not user.memory_data:
        return ""

    memory = user.memory_data if isinstance(user.memory_data, dict) else {}
    
    # Sort and pick top frequent routes
    routes = memory.get("frequent_routes", {})
    if routes:
        top_route = sorted(routes.items(), key=lambda x: x[1], reverse=True)[0][0]
        pickup, drop = top_route.split("_")
        return f"User frequently ships {pickup.title()} → {drop.title()}. Consider this if destination is ambiguous."
    
    return ""
