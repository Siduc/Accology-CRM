"""Create or update a staff_users row. Password from env only — never argv.

  STAFF_USERNAME  STAFF_PASSWORD  STAFF_ROLE  STAFF_DISPLAY_NAME
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("ENV", "development")

from app.database import SessionLocal, init_db
from app.models.staff_user import StaffUser
from app.services.staff_auth import hash_password


def main() -> int:
    username = (os.environ.get("STAFF_USERNAME") or "").strip()
    password = os.environ.get("STAFF_PASSWORD") or ""
    role = (os.environ.get("STAFF_ROLE") or "staff").strip() or "staff"
    display = (os.environ.get("STAFF_DISPLAY_NAME") or username).strip()
    if not username or not password:
        print("STAFF_USERNAME and STAFF_PASSWORD are required", file=sys.stderr)
        return 2
    if role not in ("principal", "staff", "viewer"):
        print("STAFF_ROLE must be principal, staff, or viewer", file=sys.stderr)
        return 2
    init_db()
    db = SessionLocal()
    try:
        row = db.query(StaffUser).filter(StaffUser.username == username).first()
        created = row is None
        if created:
            row = StaffUser(username=username, role=role, is_active=True)
            db.add(row)
        row.password_hash = hash_password(password)
        row.display_name = display
        row.role = role
        row.is_active = True
        db.commit()
        print(
            f"{'created' if created else 'updated'} username={username} role={role} id={row.id}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
