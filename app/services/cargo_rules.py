from app.models.enums import CargoCategory
import logging

logger = logging.getLogger(__name__)

INCOMPATIBLE = {
    "food": ["chemicals", "hazardous"],
    "fragile": ["steel", "cement"],
    "agriculture": ["chemicals", "hazardous"],
    "chemicals": ["food", "agriculture"],
    "hazardous": ["food", "consumer_goods", "agriculture"]
}

def is_cargo_compatible(load_category: str | CargoCategory | None, truck_allowed_categories: list[str] | None) -> bool:
    """Check if the load category is safe and compatible with the truck's allowed categories."""
    
    if not truck_allowed_categories:
        # Default fallback: if a truck listing has no allowed_categories (legacy or empty),
        # accept safe loads by default, but reject hazardous classes unless explicitly allowed.
        if load_category and str(load_category) in ["hazardous", "chemicals"]:
            return False
        return True
        
    if not load_category:
        return True
        
    cat_val = load_category.value if hasattr(load_category, "value") else str(load_category)
    
    # Primary Rule: load.category IN truck.allowed_categories
    if cat_val not in truck_allowed_categories:
        return False
        
    # Safety Check: Does this truck allow incompatible combinations?
    # e.g., if load is 'food', but truck allows 'chemicals', it poses a risk of cross-contamination 
    if cat_val in INCOMPATIBLE:
        for incompat_rule in INCOMPATIBLE[cat_val]:
            if incompat_rule in truck_allowed_categories:
                logger.warning(f"Safety Violation: Safe category {cat_val} blocked because truck also allows {incompat_rule}")
                return False

    return True
