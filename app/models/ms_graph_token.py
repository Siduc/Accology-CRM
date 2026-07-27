"""Microsoft Graph OAuth tokens (OneDrive delegated access)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class MsGraphToken(Base):
    """
    Practice-level Microsoft Graph token for OneDrive document storage.
    Single active connection is expected; older rows soft-revoked.
    """

    __tablename__ = "ms_graph_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, default="Bearer")
    expires_at = Column(DateTime, nullable=True)
    scope = Column(Text, nullable=True)
    ms_user_email = Column(String, nullable=True)
    ms_user_id = Column(String, nullable=True)
    drive_id = Column(String, nullable=True)
    root_folder_id = Column(String, nullable=True)
    # active | revoked | expired
    status = Column(String, default="active", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
