from app.database import SessionLocal
from app.models import Client
from sqlalchemy import func

db = SessionLocal()

duplicates = (
    db.query(Client.company_number, func.count(Client.id).label("count"))
    .group_by(Client.company_number)
    .having(func.count(Client.id) > 1)
    .all()
)

print(f"Found {len(duplicates)} company numbers with duplicates:")
for company_number, count in duplicates:
    print(f"{company_number} appears {count} times")

db.close()
