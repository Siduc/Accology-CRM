"""Practice-level Xero OAuth token (one connection, many organisations)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class XeroToken(Base):
    """Single practice Xero login. Tenants (orgs) live in tenants_json."""

    __tablename__ = "xero_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, default="Bearer")
    expires_at = Column(DateTime, nullable=True)
    scope = Column(Text, nullable=True)
    xero_user_id = Column(String, nullable=True)
    xero_email = Column(String, nullable=True)
    tenants_json = Column(Text, nullable=True)
    # active | revoked | expired
    status = Column(String, default="active", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
