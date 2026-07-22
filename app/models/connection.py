"""Per-client integration connection flags (opt-in)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class ClientConnection(Base):
    """
    Opt-in integration flags for a client.
    Missing row or enabled=false means the integration is off (default private).
    """

    __tablename__ = "client_connections"
    __table_args__ = (
        UniqueConstraint("client_id", "provider", name="uq_client_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    # asana | xero | sage | …
    provider = Column(String, index=True, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    external_id = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", backref="connections")
