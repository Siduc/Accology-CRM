"""Companies House OAuth access/refresh tokens (API Filing)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class ChOAuthToken(Base):
    """
    Practice-stored OAuth tokens from Companies House Identity Service.

    Per-company rows are created when scopes include a company number
    (user supplies company authentication code on the CH consent screen).
    Practice-level profile tokens have client_id/company_number null.
    """

    __tablename__ = "ch_oauth_tokens"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    company_number = Column(String, nullable=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, default="Bearer")
    expires_at = Column(DateTime, nullable=True)
    scope = Column(Text, nullable=True)
    ch_user_email = Column(String, nullable=True)
    ch_user_id = Column(String, nullable=True)
    # active | revoked | expired
    status = Column(String, default="active", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
