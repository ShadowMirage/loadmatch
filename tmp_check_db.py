from app.database import SessionLocal
from app.models.processed_message import ProcessedMessage

db = SessionLocal()
pms = db.query(ProcessedMessage).all()
print(f"Total ProcessedMessages in DB: {len(pms)}")
for p in pms:
    print(f"ID: {p.id} | Key: {p.idempotency_key} | Status: {p.status} | Delivery: {p.delivery_state} | Delivered At: {p.delivered_at}")
