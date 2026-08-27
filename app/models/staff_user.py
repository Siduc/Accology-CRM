from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.database import Base

ROLES = ("principal", "staff", "viewer")


class StaffUser(Base):
    """Practice login. Replaces the single shared AUTH_USERNAME/PASSWORD over time."""

    __tablename__ = "staff_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    role = Column(String, nullable=False, default="staff")  # principal | staff | viewer
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
