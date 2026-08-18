"""Per-client accounts-production playbook (source, IRIS, year end, approval)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import backref, relationship

from app.database import Base

BOOKKEEPING_SOURCES = (
    ("xero", "Xero"),
    ("sage50", "Sage 50 (desktop)"),
    ("sage_cloud", "Sage Business Cloud"),
    ("qbo", "QuickBooks Online"),
    ("client_tb", "Client trial balance (IRIS recode)"),
    ("bank_csv", "Bank CSV only"),
    ("other", "Other / mixed"),
)

SOURCE_CODES = {c for c, _ in BOOKKEEPING_SOURCES}


class ClientPlaybook(Base):
    """How this client's year-end is actually done."""

    __tablename__ = "client_playbooks"
    __table_args__ = (UniqueConstraint("client_id", name="uq_client_playbook"),)

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)

    bookkeeping_source = Column(String, nullable=True, default="xero")
    source_org_id = Column(String, nullable=True)
    source_notes = Column(Text, nullable=True)

    iris_client_code = Column(String, nullable=True)
    iris_notes = Column(Text, nullable=True)

    year_end_month = Column(Integer, nullable=True)
    year_end_day = Column(Integer, nullable=True)
    current_year = Column(Integer, nullable=True)

    approver_name = Column(String, nullable=True)
    approver_email = Column(String, nullable=True)
    approval_notes = Column(Text, nullable=True)

    quirks = Column(Text, nullable=True)

    folders_ensured_at = Column(DateTime, nullable=True)
    agents_md_written_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", backref=backref("playbook", uselist=False))
