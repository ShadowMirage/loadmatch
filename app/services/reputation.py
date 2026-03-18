from sqlalchemy.orm import Session
from app.models.user import User

def compute_reputation(user: User, db: Session) -> float:
    """
    Computes a 0.0 - 5.0 rating dynamically for a user.
    Formula: (completion_rate * 0.4) + ((1 - cancellation_rate) * 0.3) + (rating * 0.3)
    """
    if not user:
        return 5.0
        
    # Default values for new users
    completion_rate = user.completion_rate if user.completion_rate is not None else 1.0
    cancellation_rate = user.cancellation_rate if user.cancellation_rate is not None else 0.0
    rating = user.rating if user.rating is not None else 5.0

    # We assume completion and cancellation rates are 0.0 to 1.0
    # The final rating should be out of 5.0. 
    # Since completion/cancellation are typically percentages, we'll scale them to 5.0
    comp_score = (completion_rate * 5.0) * 0.4
    canc_score = ((1.0 - cancellation_rate) * 5.0) * 0.3
    rat_score = (rating) * 0.3

    final_score = comp_score + canc_score + rat_score
    
    # Cap to max 5.0, min 1.0
    final_score = max(1.0, min(5.0, final_score))
    
    # Update DB caching
    user.rating = final_score
    db.commit()
    
    return final_score
