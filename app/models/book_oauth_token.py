"""Practice OAuth tokens for Sage Business Cloud and QuickBooks Online."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class BookOauthToken(Base):
    """One active row per provider (sage | qbo). Tenants in tenants_json."""

    __tablename__ = "book_oauth_tokens"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)  # sage | qbo
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, default="Bearer")
    expires_at = Column(DateTime, nullable=True)
    scope = Column(Text, nullable=True)
    user_email = Column(String, nullable=True)
    user_id = Column(String, nullable=True)
    tenants_json = Column(Text, nullable=True)
    status = Column(String, default="active", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
